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

CATATAN PERFORMA: seluruh `asyncio.sleep()` buta di file ini sudah diganti
dengan menunggu kondisi nyata lewat helper di perf.py. Detail alasannya
ada di docstring perf.py.
"""

import asyncio
import os
import re

from playwright.async_api import Page

from config import (
    TIKTOK_EMAIL,
    TIKTOK_PASSWORD,
    FAST_TIMEOUT,
    SLOW_TIMEOUT,
    STORAGE_STATE_PATH,
)
from otp_utils import wait_for_new_otp, latest_tiktok_uid
from perf import click_first, first_match

LOGIN_SCREENSHOT_PATH = "tiktok_login_success.png"

# Penanda bahwa halaman sudah dalam keadaan login.
#
# Versi lama memakai satu string `"text=Buat, a[href*='/creator/']"`.
# Itu rapuh karena engine `text=` mencocokkan SUBSTRING: elemen apa pun
# yang memuat kata "Buat" ikut cocok (mis. "Buat efek", "Dibuat oleh"),
# termasuk elemen yang bisa muncul saat BELUM login. Sekarang dipecah jadi
# daftar selector, diprioritaskan yang berbasis `data-e2e`/href (stabil,
# tidak tergantung bahasa), dan pencocokan teksnya dibuat eksak lewat regex.
LOGGED_IN_SELECTORS = [
    '[data-e2e="profile-icon"]',
    '[data-e2e="nav-upload"]',
    "a[href*='/upload']",
    "a[href*='/creator/']",
    "text=/^Buat$/i",
    "text=/^Upload$/i",
]

# Halaman "Verifikasi bahwa ini memang Anda".
VERIFY_SELECTORS = [
    "text=/Verifikasi bahwa ini memang Anda/i",
    "text=/[Vv]erify.*(it'?s|that it'?s) you/i",
]

# Captcha (selector sama persis dengan yang dipakai captcha_solver.py).
CAPTCHA_SELECTORS = ['.captcha-verify-img-slide, .captcha-verify, div[class*="captcha"]']

# Pesan error kredensial. Dipakai supaya password salah langsung ketahuan
# alih-alih menunggu timeout OTP yang panjang.
LOGIN_ERROR_SELECTORS = [
    "text=/Kata sandi.*salah/i",
    "text=/[Ii]ncorrect password/i",
    "text=/Akun tidak ditemukan/i",
    "text=/[Cc]ouldn'?t find (your )?account/i",
    "text=/Terlalu banyak percobaan/i",
    "text=/[Tt]oo many attempts/i",
]

# Pesan penolakan OTP. Versi lama tidak pernah memeriksa ini - script
# mengisi kode, mengklik verifikasi, lalu lanjut seolah berhasil walaupun
# TikTok menampilkan "Kode verifikasi kedaluwarsa atau salah".
OTP_ERROR_SELECTORS = [
    "text=/Kode verifikasi kedaluwarsa atau salah/i",
    "text=/[Kk]ode.*(salah|kedaluwarsa|tidak valid)/i",
    "text=/[Cc]ode is (incorrect|expired|invalid)/i",
    "text=/[Ii]ncorrect (verification )?code/i",
]


# ===== LOGOUT =====
# Menu profil hanya ada di layout desktop. Di emulasi mobile (yang dipakai
# untuk login) header-nya disederhanakan dan ikon ini tidak dirender.
# HANYA pemicu dropdown avatar. `[data-e2e="nav-profile"]` sengaja TIDAK
# dipakai: itu link "Profil" di sidebar yang tetap dirender walau sesi
# anonim, dan mengkliknya memicu navigasi - bukan membuka menu keluar.
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

# Sebagian versi UI menampilkan dialog konfirmasi setelah klik "Keluar".
LOGOUT_CONFIRM_SELECTORS = [
    'button:has-text("Keluar")',
    'button:has-text("Log out")',
    'button:has-text("Ya")',
]

# Penanda bahwa sesi benar-benar sudah berakhir.
LOGGED_OUT_SELECTORS = [
    '[data-e2e="top-login-button"]',
    'button:has-text("Log masuk")',
    'button:has-text("Log in")',
]


async def _wait_for_any(page: Page, selectors, timeout: int = 5000) -> bool:
    """
    Tunggu sampai salah satu dari beberapa selector muncul.

    Versi lama memakai `asyncio.gather(..., return_exceptions=True)` yang
    menunggu SELURUH probe selesai. Akibatnya biaya fungsi ini selalu
    sebesar `timeout` penuh - walaupun selector pertama sudah cocok dalam
    50 ms, ia tetap menunggu probe lain habis timeout. Di alur login lama
    fungsi ini dipanggil 3x, jadi ~25 detik terbuang percuma tiap run.

    Sekarang didelegasikan ke perf.first_match() yang langsung return
    begitu ada yang cocok.
    """
    return await first_match(page, selectors, timeout=timeout) is not None


async def _try_click(page: Page, selectors, timeout: int = 5000, has_text=None) -> bool:
    """
    Coba klik elemen pertama yang cocok dari daftar `selectors`.
    Tidak pernah melempar exception - return True/False. Ini menghindari
    bug lama di mana fallback selector yang gagal ikut membuat seluruh
    script crash (bukan fallback ke manual).

    Versi lama mencoba selector satu per satu dengan timeout PENUH untuk
    masing-masing, jadi 4 selector yang tidak ada = 20 detik terbuang.
    Sekarang seluruh daftar disapu tiap siklus polling, dan `timeout`
    berlaku untuk keseluruhan pencarian - bukan per selector.
    """
    return await click_first(page, selectors, timeout=timeout, has_text=has_text)


async def _dismiss_app_popup(page: Page):
    # Timeout dipendekkan: pop-up ini kalau muncul, muncul segera setelah
    # halaman render. Menunggu 3 detik untuk sesuatu yang sering tidak ada
    # sama sekali adalah biaya tetap yang tidak perlu.
    clicked = await _try_click(
        page,
        ['[data-e2e="bottom-cta-cancel-btn"]', "text=/Nanti saja/i", "text=/Not now/i"],
        timeout=1500,
    )
    if clicked:
        print("[LOGIN] Pop-up ditutup.")
    else:
        print("[LOGIN] Tidak ada pop-up 'buka app' yang perlu ditutup.")


async def _open_email_login_form(page: Page):
    # Pilih opsi "gunakan telepon/email/username" di layar login awal.
    # data-e2e="channel-item" dipakai beberapa pilihan (Google, Apple, dst),
    # jadi disaring lewat teksnya (dwibahasa) untuk ambil yang benar.
    clicked = await _try_click(
        page,
        ['[data-e2e="channel-item"]'],
        timeout=SLOW_TIMEOUT,
        has_text=re.compile(r"phone|email|telepon|nomor", re.I),
    )
    if not clicked:
        # Fallback lama, siapa tahu struktur data-e2e berubah.
        clicked = await _try_click(
            page, ["text=/.*nomor telepon.*email.*/i", "text=/Telepon/i"], timeout=FAST_TIMEOUT
        )
    if clicked:
        print("[LOGIN] Opsi login (telepon/email) dipilih.")
    else:
        print("[WARNING] Tidak menemukan opsi 'telepon/email/username'. Mungkin sudah di form yang benar.")

    # Pengganti `asyncio.sleep(2)`: tunggu form-nya benar-benar dirender.
    # Kalau tab email sudah aktif duluan, input password sudah ada dan kita
    # langsung lanjut tanpa menunggu sisa jeda.
    await first_match(
        page,
        ['[data-e2e="email-tab"]', 'input[type="password"]', 'input[name="username"]'],
        timeout=SLOW_TIMEOUT,
    )

    # Pindah ke tab "Email / Username" (kalau tab "Phone" yang aktif duluan).
    clicked = await _try_click(
        page,
        ['[data-e2e="email-tab"]', "text=/Alamat email/i", "text=/Email.*Username/i"],
        timeout=1500,
    )
    if clicked:
        print("[LOGIN] Tab 'Email/Username' diklik.")
    else:
        print("[INFO] Tab 'Email/Username' tidak ditemukan (mungkin sudah aktif).")


async def _fill_credentials(page: Page) -> bool:
    # `fill()` sudah menunggu elemen siap dipakai (actionability check),
    # jadi `asyncio.sleep(1)` di antara langkah-langkah ini murni jeda mati.
    try:
        email_input = page.locator('input[name="username"], input[type="text"], input[type="email"]').first
        await email_input.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        await email_input.fill(TIKTOK_EMAIL)
        print(f"[LOGIN] Email terisi: {TIKTOK_EMAIL}")
    except Exception as e:
        print(f"[ERROR] Gagal isi email: {e}")
        return False

    try:
        pass_input = page.locator('input[type="password"]').first
        await pass_input.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        await pass_input.fill(TIKTOK_PASSWORD)
        print("[LOGIN] Password terisi.")
    except Exception as e:
        print(f"[ERROR] Gagal isi password: {e}")
        return False

    clicked = await _try_click(
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
    """
    Cari field OTP. Versi lama mencoba 3 selector BERURUTAN dengan timeout
    5 detik masing-masing - kalau yang cocok adalah yang ketiga, biayanya
    10 detik menunggu dua yang pertama gagal. Sekarang ketiganya disapu
    bersamaan lewat first_match().
    """
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


async def _handle_otp_verification(page: Page):
    detected = await _wait_for_any(page, VERIFY_SELECTORS, timeout=FAST_TIMEOUT)
    if detected:
        print("[LOGIN] Halaman verifikasi terdeteksi.")
    else:
        print("[WARNING] Tidak ada halaman verifikasi terdeteksi otomatis.")
        input("Periksa browser. Jika sudah login, tekan Enter untuk lanjut...")
        return

    # GARIS BATAS INBOX - dicatat SEBELUM kode diminta.
    #
    # Ini perbaikan untuk "Kode verifikasi kedaluwarsa atau salah". Tanpa
    # garis batas, polling akan mengambil email TikTok terbaru yang ADA
    # SAAT ITU - yang biasanya adalah kode dari permintaan sebelumnya,
    # karena email kode yang baru butuh beberapa detik untuk sampai.
    # Kode lama itu otomatis hangus begitu TikTok menerbitkan kode baru.
    baseline_uid = await asyncio.to_thread(latest_tiktok_uid)
    if baseline_uid:
        print(f"[OTP] Garis batas inbox dicatat: UID {baseline_uid}")
    else:
        print("[OTP] Garis batas inbox tidak terbaca (inbox kosong / IMAP gagal).")

    clicked = await _try_click(
        page,
        ["text=/Alamat email/i", "text=/Email/i", "text=/.*@gmail\\.com/i"],
        timeout=FAST_TIMEOUT,
    )
    if clicked:
        print("[LOGIN] Opsi kirim OTP ke email diklik.")
    else:
        print("[WARNING] Gagal klik otomatis. Klik manual.")
        input("Klik 'Alamat email'/'Email' manual, lalu Enter...")

    # Race: halaman OTP muncul, ATAU ternyata sudah langsung login.
    # Versi lama mengecek keduanya berurutan (15 detik + 3 detik).
    outcome = await first_match(
        page,
        ["text=/Kirim ulang kode/i", "text=/[Rr]esend code/i"] + LOGGED_IN_SELECTORS,
        timeout=SLOW_TIMEOUT + 5000,
    )
    if outcome is None:
        print("[WARNING] Halaman OTP tidak muncul dan belum terlihat login.")
    elif outcome >= 2:
        print("[LOGIN] Sudah login, lewati OTP.")
        return
    else:
        print("[LOGIN] Halaman OTP muncul.")

    otp_input = await _find_otp_input(page)
    if otp_input is None:
        print("[ERROR] Field OTP tidak ditemukan. Isi manual.")
        input("Isi OTP manual di browser, lalu tekan Enter...")
        return

    print("\n[OTP] Menunggu OTP terbaru...")
    # PENTING: wait_for_new_otp() sinkron dan memakai time.sleep(), yang
    # akan MEMBEKUKAN seluruh event loop asyncio selama ia berjalan (dulu
    # sampai 60 detik). Selama itu browser tidak bisa merespons apa pun.
    # Dipindah ke thread terpisah supaya event loop tetap hidup.
    # `after_uid` memastikan hanya email yang datang SETELAH kode diminta
    # yang diterima - lihat catatan garis batas di atas.
    otp_code = await asyncio.to_thread(wait_for_new_otp, after_uid=baseline_uid)

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
        timeout=FAST_TIMEOUT,
    )
    if clicked:
        print("[LOGIN] Tombol verifikasi diklik.")
    else:
        print("[INFO] Tombol verifikasi tidak ditemukan (mungkin OTP auto-submit).")

    # Pengganti `asyncio.sleep(3)`: race hasilnya - berhasil login, atau
    # kode ditolak. Versi lama tidak pernah memeriksa penolakan, jadi
    # script lanjut seolah berhasil padahal layar masih menampilkan
    # "Kode verifikasi kedaluwarsa atau salah".
    outcome = await first_match(
        page, LOGGED_IN_SELECTORS + OTP_ERROR_SELECTORS, timeout=SLOW_TIMEOUT
    )
    if outcome is not None and outcome >= len(LOGGED_IN_SELECTORS):
        print("[ERROR] TikTok menolak kode: kedaluwarsa atau salah.")
        print("[ERROR] Kode yang dipakai kemungkinan berasal dari email OTP lama.")
        print("[ERROR] Tunggu hitung mundur 'Kirim ulang kode' selesai, lalu jalankan ulang.")
        input("Atau selesaikan verifikasi manual di browser, lalu tekan Enter...")
    elif outcome is None:
        print("[WARNING] Status verifikasi tidak jelas - periksa jendela browser.")


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
    # tidak valid) sebelum login penuh - cookies "setengah valid" bisa
    # membuat TikTok menampilkan layar "lanjutkan sebagai..." alih-alih
    # chooser login normal, sehingga selector di bawah gagal semua.
    try:
        await page.context.clear_cookies()
    except Exception:
        pass

    # "networkidle" dihindari - lihat catatan di is_logged_in().
    await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=30000)

    # `asyncio.sleep(2)` setelah goto dihapus: _dismiss_app_popup() sudah
    # melakukan polling sendiri, jadi ia otomatis menunggu selama memang
    # perlu dan langsung lanjut kalau halaman sudah siap.
    await _dismiss_app_popup(page)
    await _open_email_login_form(page)

    if not await _fill_credentials(page):
        return False

    # Pengganti `asyncio.sleep(3)` + pengecekan berurutan: race semua
    # kemungkinan hasil sekaligus, lalu bercabang berdasarkan yang menang.
    # Ini juga membuat password salah langsung ketahuan, bukan menggantung
    # sampai timeout OTP.
    n_ok = len(LOGGED_IN_SELECTORS)
    n_verify = len(VERIFY_SELECTORS)
    outcome = await first_match(
        page,
        LOGGED_IN_SELECTORS + VERIFY_SELECTORS + CAPTCHA_SELECTORS + LOGIN_ERROR_SELECTORS,
        timeout=SLOW_TIMEOUT,
    )

    if outcome is not None and outcome < n_ok:
        print("[LOGIN] Login berhasil (tanpa verifikasi).")
    elif outcome is not None and outcome >= n_ok + n_verify + len(CAPTCHA_SELECTORS):
        print("[ERROR] Login ditolak (kredensial salah / terlalu banyak percobaan).")
        print("[ERROR] Periksa TIKTOK_EMAIL & TIKTOK_PASSWORD di .env.")
        return False
    else:
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
        return await first_match(page, LOGGED_IN_SELECTORS, timeout=FAST_TIMEOUT + 2000) is not None
    except Exception:
        return False


def clear_session_file(reason: str = "") -> bool:
    """
    Hapus file sesi tersimpan supaya run berikutnya login dari nol.

    Dipakai HANYA untuk dua hal: setelah logout disengaja, dan saat sesi
    terdeteksi rusak. Jangan dipanggil rutin - sesi yang sehat justru yang
    membuat script bisa melewati login, OTP, dan captcha.
    """
    if not os.path.exists(STORAGE_STATE_PATH):
        return False
    try:
        os.remove(STORAGE_STATE_PATH)
        suffix = f" ({reason})" if reason else ""
        print(f"[SESI] File {STORAGE_STATE_PATH} dihapus{suffix}.")
        return True
    except Exception as e:
        print(f"[SESI] Gagal menghapus {STORAGE_STATE_PATH}: {e}")
        return False


async def logout_tiktok(page: Page) -> bool:
    """
    Keluar dari akun lewat menu profil di header.

    Harus dipanggil pada page dengan layout DESKTOP - di emulasi mobile
    ikon profilnya tidak dirender sama sekali.

    Return True kalau status keluar terkonfirmasi (tombol 'Log masuk'
    muncul kembali). Tidak pernah melempar exception.
    """
    print("\n[LOGOUT] Keluar dari akun...")

    # Pemeriksaan "sudah tidak login" HARUS di depan. Kalau ditaruh di
    # belakang sebagai fallback, script terlanjur mencoba membuka menu
    # profil dan bisa mengklik elemen lain yang mirip, lalu melaporkan
    # gagal padahal memang tidak ada yang perlu dikerjakan.
    if await first_match(page, LOGGED_OUT_SELECTORS, timeout=1200) is not None:
        print("[LOGOUT] Sudah tidak dalam keadaan login, tidak ada yang perlu dilakukan.")
        return True

    opened = await click_first(page, PROFILE_MENU_SELECTORS, timeout=FAST_TIMEOUT)
    if not opened:
        # Halaman video kadang menyembunyikan header saat overlay terbuka.
        # Balik ke homepage sekali, lalu coba lagi.
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

    # Dialog konfirmasi tidak selalu ada, jadi kegagalan di sini diabaikan.
    await click_first(page, LOGOUT_CONFIRM_SELECTORS, timeout=1500)

    confirmed = await first_match(page, LOGGED_OUT_SELECTORS, timeout=SLOW_TIMEOUT) is not None
    if confirmed:
        print("[LOGOUT] Berhasil keluar dari akun.")
    else:
        print("[LOGOUT] Status keluar tidak terkonfirmasi - periksa jendela browser.")
    return confirmed


async def ensure_logged_in(page: Page) -> bool:
    """
    Pastikan page dalam keadaan login: cek dulu apakah sesi yang sudah
    dimuat (lewat storage_state saat membuat context) masih valid. Kalau
    masih valid, login penuh (isi email/password/OTP) dilewati sama
    sekali - ini yang memangkas frekuensi login baru, sehingga verifikasi
    OTP/captcha juga jadi lebih jarang muncul. Kalau sesi tidak ada atau
    sudah kedaluwarsa, baru jalankan login_tiktok() seperti biasa.
    """
    if await is_logged_in(page):
        print("[LOGIN] Sesi tersimpan masih valid, login dilewati.")
        return True

    print("[LOGIN] Sesi tidak ada/sudah tidak valid, melakukan login penuh...")
    return await login_tiktok(page)
