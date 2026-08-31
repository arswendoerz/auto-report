"""
tiktok_login.py - Alur login TikTok (email + password + verifikasi OTP).

Sebelumnya alur ini diduplikasi persis di main.py dan di mode standalone
report.py. Sekarang disatukan di satu fungsi `login_tiktok(page)` supaya
perbaikan selector cukup dilakukan di satu tempat dan kedua entry point
(main.py, report.py) selalu berperilaku sama.

CATATAN BAHASA: TikTok kadang menampilkan UI login dalam Bahasa Indonesia,
kadang Bahasa Inggris, tidak konsisten walau context Playwright di-set
locale="id-ID". Karena itu selector di file ini selalu diprioritaskan
memakai atribut `data-e2e` (stabil, tidak tergantung bahasa) dan baru
fallback ke pencocokan teks dwibahasa (ID + EN) kalau atributnya tidak ada.
"""

import asyncio
import re

from playwright.async_api import Page

from config import TIKTOK_EMAIL, TIKTOK_PASSWORD
from otp_utils import wait_for_new_otp

LOGIN_SCREENSHOT_PATH = "tiktok_login_success.png"


async def _wait_for_any(page: Page, selectors, timeout: int = 5000) -> bool:
    """
    Tunggu sampai salah satu dari beberapa selector muncul, dijalankan
    CONCURRENT (bukan digabung jadi satu string dipisah koma). Menggabungkan
    beberapa `text=/regex/i` dengan koma dalam satu string ternyata bisa
    membuat Playwright gagal parse SELURUH selector kalau salah satu regex-nya
    mengandung tanda kurung/apostrof — bukan cuma bagian itu yang di-skip,
    tapi seluruh pencarian jadi tidak pernah match sama sekali.
    """
    results = await asyncio.gather(
        *[page.wait_for_selector(sel, timeout=timeout) for sel in selectors],
        return_exceptions=True,
    )
    return any(not isinstance(r, Exception) for r in results)


async def _try_click(page: Page, selectors, timeout: int = 5000, has_text=None) -> bool:
    """
    Coba klik elemen pertama yang cocok dari daftar `selectors`, satu per
    satu, sampai salah satu berhasil. Tidak pernah melempar exception —
    return True/False. Ini menghindari bug lama di mana fallback selector
    yang gagal ikut membuat seluruh script crash (bukan fallback ke manual).
    """
    if isinstance(selectors, str):
        selectors = [selectors]
    for sel in selectors:
        try:
            locator = page.locator(sel, has_text=has_text) if has_text else page.locator(sel)
            await locator.first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _dismiss_app_popup(page: Page):
    clicked = await _try_click(
        page,
        ['[data-e2e="bottom-cta-cancel-btn"]', "text=/Nanti saja/i", "text=/Not now/i"],
        timeout=3000,
    )
    if clicked:
        print("[LOGIN] Pop-up ditutup.")
        await asyncio.sleep(1)
    else:
        print("[LOGIN] Tidak ada pop-up 'buka app' yang perlu ditutup.")


async def _open_email_login_form(page: Page):
    # Pilih opsi "gunakan telepon/email/username" di layar login awal.
    # data-e2e="channel-item" dipakai beberapa pilihan (Google, Apple, dst),
    # jadi disaring lewat teksnya (dwibahasa) untuk ambil yang benar.
    clicked = await _try_click(
        page,
        ['[data-e2e="channel-item"]'],
        has_text=re.compile(r"phone|email|telepon|nomor", re.I),
    )
    if not clicked:
        # Fallback lama, siapa tahu struktur data-e2e berubah.
        clicked = await _try_click(page, ["text=/.*nomor telepon.*email.*/i", "text=/Telepon/i"])
    if clicked:
        print("[LOGIN] Opsi login (telepon/email) dipilih.")
    else:
        print("[WARNING] Tidak menemukan opsi 'telepon/email/username'. Mungkin sudah di form yang benar.")
    await asyncio.sleep(2)

    # Pindah ke tab "Email / Username" (kalau tab "Phone" yang aktif duluan).
    clicked = await _try_click(page, ['[data-e2e="email-tab"]', "text=/Alamat email/i", "text=/Email.*Username/i"])
    if clicked:
        print("[LOGIN] Tab 'Email/Username' diklik.")
        await asyncio.sleep(1)
    else:
        print("[INFO] Tab 'Email/Username' tidak ditemukan (mungkin sudah aktif).")


async def _fill_credentials(page: Page) -> bool:
    try:
        email_input = page.locator('input[name="username"], input[type="text"], input[type="email"]').first
        await email_input.wait_for(state="visible", timeout=10000)
        await email_input.fill(TIKTOK_EMAIL)
        print(f"[LOGIN] Email terisi: {TIKTOK_EMAIL}")
    except Exception as e:
        print(f"[ERROR] Gagal isi email: {e}")
        return False
    await asyncio.sleep(1)

    try:
        pass_input = page.locator('input[type="password"]').first
        await pass_input.wait_for(state="visible", timeout=10000)
        await pass_input.fill(TIKTOK_PASSWORD)
        print("[LOGIN] Password terisi.")
    except Exception as e:
        print(f"[ERROR] Gagal isi password: {e}")
        return False
    await asyncio.sleep(1)

    clicked = await _try_click(
        page,
        [
            '[data-e2e="login-button"]:not([disabled])',
            'button:has-text("Log masuk"):not([disabled])',
            'button:has-text("Log in"):not([disabled])',
            'button[type="submit"]:not([disabled])',
        ],
    )
    if clicked:
        print("[LOGIN] Tombol login diklik.")
    else:
        print("[ERROR] Tombol login tidak ditemukan/tidak bisa diklik.")
        return False

    await asyncio.sleep(3)
    return True


async def _find_otp_input(page: Page):
    for selector, label in [
        ('#code-input', '#code-input'),
        ('.components-code-input .input', 'class'),
        ('input[type="text"][maxlength="6"]', 'maxlength'),
    ]:
        try:
            otp_input = page.locator(selector).first
            await otp_input.wait_for(state="visible", timeout=5000)
            print(f"[LOGIN] Field OTP ditemukan ({label}).")
            return otp_input
        except Exception:
            continue
    return None


async def _handle_otp_verification(page: Page):
    detected = await _wait_for_any(
        page,
        [
            "text=/Verifikasi bahwa ini memang Anda/i",
            "text=/[Vv]erify.*(it'?s|that it'?s) you/i",
        ],
        timeout=5000,
    )
    if detected:
        print("[LOGIN] Halaman verifikasi terdeteksi.")
    else:
        print("[WARNING] Tidak ada halaman verifikasi terdeteksi otomatis.")
        input("Periksa browser. Jika sudah login, tekan Enter untuk lanjut...")
        return

    clicked = await _try_click(
        page,
        ["text=/Alamat email/i", "text=/Email/i", "text=/.*@gmail\\.com/i"],
    )
    if clicked:
        print("[LOGIN] Opsi kirim OTP ke email diklik.")
    else:
        print("[WARNING] Gagal klik otomatis. Klik manual.")
        input("Klik 'Alamat email'/'Email' manual, lalu Enter...")

    otp_page_shown = await _wait_for_any(
        page,
        ["text=/Kirim ulang kode/i", "text=/[Rr]esend code/i"],
        timeout=15000,
    )
    if otp_page_shown:
        print("[LOGIN] Halaman OTP muncul.")
    else:
        try:
            await page.wait_for_selector("text=Buat, a[href*='/creator/']", timeout=3000)
            print("[LOGIN] Sudah login, lewati OTP.")
            return
        except Exception:
            print("[WARNING] Halaman OTP tidak muncul dan belum terlihat login.")

    otp_input = await _find_otp_input(page)
    if otp_input is None:
        print("[ERROR] Field OTP tidak ditemukan. Isi manual.")
        input("Isi OTP manual di browser, lalu tekan Enter...")
        return

    print("\n[OTP] Menunggu OTP terbaru...")
    otp_code = wait_for_new_otp()

    if not otp_code:
        otp_code = input("Gagal ambil OTP otomatis. Ketik OTP 6 digit manual: ").strip()
        if not otp_code or len(otp_code) != 6 or not otp_code.isdigit():
            print("[ERROR] OTP tidak valid. Selesaikan login manual di browser.")
            input("Tekan Enter setelah login manual selesai...")
            return

    try:
        await otp_input.fill(otp_code)
        print(f"[LOGIN] OTP {otp_code} diisi.")
    except Exception as e:
        print(f"[ERROR] Gagal isi OTP: {e}")
        input("Isi manual di browser, lalu Enter...")

    clicked = await _try_click(
        page,
        [
            'button:has-text("Verifikasi")',
            'button:has-text("Lanjutkan")',
            'button:has-text("Verify")',
            'button:has-text("Continue")',
            'button:has-text("Submit")',
        ],
    )
    if clicked:
        print("[LOGIN] Tombol verifikasi diklik.")
    else:
        print("[INFO] Tombol verifikasi tidak ditemukan (mungkin OTP auto-submit).")

    await asyncio.sleep(3)


async def login_tiktok(page: Page) -> bool:
    """
    Login ke TikTok dengan email + password, menangani verifikasi OTP
    (ambil otomatis dari Gmail, fallback ke input manual bila gagal).

    Return True jika alur login selesai dijalankan (baik berhasil otomatis
    maupun diselesaikan manual oleh pengguna). Return False hanya jika
    form email/password gagal total dan tidak bisa dilanjutkan.
    """
    print("\n[LOGIN] Membuka halaman login...")
    # Buang cookies lama (kalau ada sisa dari storage_state yang sudah
    # tidak valid) sebelum login penuh — cookies "setengah valid" bisa
    # membuat TikTok menampilkan layar "lanjutkan sebagai..." alih-alih
    # chooser login normal, sehingga selector di bawah gagal semua.
    try:
        await page.context.clear_cookies()
    except Exception:
        pass

    # "networkidle" dihindari — lihat catatan di is_logged_in().
    await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)

    await _dismiss_app_popup(page)
    await _open_email_login_form(page)

    if not await _fill_credentials(page):
        return False

    try:
        await page.wait_for_selector("text=Buat, a[href*='/creator/']", timeout=5000)
        print("[LOGIN] Login berhasil (tanpa verifikasi).")
    except Exception:
        print("[LOGIN] Ada verifikasi, proses OTP...")
        await _handle_otp_verification(page)

    try:
        await page.screenshot(path=LOGIN_SCREENSHOT_PATH)
        print(f"[LOGIN] Screenshot disimpan: {LOGIN_SCREENSHOT_PATH}")
    except Exception:
        pass

    print("[LOGIN] Proses login selesai.")
    return True


async def is_logged_in(page: Page) -> bool:
    """
    Cek apakah page saat ini sudah dalam keadaan login, TANPA mencoba
    login. Dipakai untuk memeriksa apakah sesi yang dimuat dari
    storage_state (cookies tersimpan) masih valid.
    """
    try:
        # "networkidle" dihindari: TikTok terus kirim request background
        # (ads/analytics) yang jarang benar-benar hening, jadi wait_until
        # "networkidle" sering timeout 30 detik walau halaman sudah siap.
        await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector("text=Buat, a[href*='/creator/']", timeout=5000)
        return True
    except Exception:
        return False


async def ensure_logged_in(page: Page) -> bool:
    """
    Pastikan page dalam keadaan login: cek dulu apakah sesi yang sudah
    dimuat (lewat storage_state saat membuat context) masih valid. Kalau
    masih valid, login penuh (isi email/password/OTP) dilewati sama
    sekali — ini yang memangkas frekuensi login baru, sehingga verifikasi
    OTP/captcha juga jadi lebih jarang muncul. Kalau sesi tidak ada atau
    sudah kedaluwarsa, baru jalankan login_tiktok() seperti biasa.
    """
    if await is_logged_in(page):
        print("[LOGIN] Sesi tersimpan masih valid, login dilewati.")
        return True

    print("[LOGIN] Sesi tidak ada/sudah tidak valid, melakukan login penuh...")
    return await login_tiktok(page)
