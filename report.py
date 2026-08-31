import asyncio
import os
import re
import sys

from playwright.async_api import Page, Browser, BrowserContext, async_playwright

from config import (
    TARGET_VIDEO_URL,
    STORAGE_STATE_PATH,
    FAST_TIMEOUT,
    SLOW_TIMEOUT,
    HEADLESS,
    BLOCK_HEAVY_RESOURCES,
    BLOCK_IMAGES,
    PAUSE_AT_END,
    LOGOUT_AFTER_REPORT,
    AUTO_CLEAR_STALE_SESSION,
)
from captcha_solver import check_and_solve_captcha
from perf import (
    Stopwatch,
    block_heavy_resources,
    click_first,
    first_match,
    panel_signature,
    wait_panel_change,
)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')


# Selector captcha, disamakan persis dengan yang dipakai captcha_solver.py.
CAPTCHA_SELECTOR = '.captcha-verify-img-slide, .captcha-verify, div[class*="captcha"]'

MORE_MENU_SELECTOR = 'button[data-e2e="more-menu-icon"]'
REPORT_ITEM_SELECTOR = '[data-e2e="more-menu-popover_report"]'
REASON_LABEL_SELECTOR = 'label[data-e2e="report-card-reason"]'

APP_POPUP_SELECTORS = [
    '[data-e2e="bottom-cta-cancel-btn"]',
    "text=/Nanti saja/i",
    "text=/Not now/i",
]

# Halaman video menampilkan tombol "Log masuk" = sesi TIDAK dikenali di
# halaman ini, walaupun cek sesi di homepage sempat lolos. Tanpa deteksi
# ini script akan terus mencari menu "..." yang memang tidak akan pernah
# ada, lalu berhenti dengan pesan yang menyesatkan.
LOGIN_WALL_SELECTORS = [
    '[data-e2e="top-login-button"]',
    'button:has-text("Log masuk")',
    'button:has-text("Log in")',
]

# Penanda bahwa laporan benar-benar terkirim. Versi lama berhenti dengan
# "cek manual di browser" tanpa pernah memastikan apa pun.
REPORT_DONE_SELECTORS = [
    "text=/Terima kasih atas laporan/i",
    "text=/Thanks for (your report|reporting)/i",
    "text=/Laporan.*(terkirim|diterima)/i",
    "text=/[Rr]eport (submitted|received)/i",
]


class StaleSessionError(RuntimeError):
    """
    Halaman video menolak sesi yang dipakai.

    Dibedakan dari kegagalan biasa karena penanganannya beda: kegagalan
    lain layak dicoba ulang, sedangkan sesi yang ditolak tidak akan pernah
    berhasil berapa kali pun diulang - cookies-nya harus dibuang dulu dan
    login diulang dari nol.
    """


async def _maybe_solve_captcha(page: Page) -> bool:
    """
    Probe cepat sebelum memanggil penanganan captcha.

    Versi lama selalu membayar `wait_for_selector(timeout=3000)` penuh di
    jalur normal - padahal di sebagian besar run tidak ada captcha sama
    sekali, jadi 3 detik itu murni hangus setiap kali. Sekarang dicek
    dengan probe 600 ms; kalau tidak ada, langsung lanjut.

    Logika penyelesaian captcha-nya sendiri TIDAK diubah - tetap dipanggil
    apa adanya dari captcha_solver.check_and_solve_captcha().
    """
    if await first_match(page, [CAPTCHA_SELECTOR], timeout=600) is None:
        return False
    return await check_and_solve_captcha(page)


async def close_tiktok_app_popup(page: Page):
    """Tutup pop-up 'Tonton video ini di TikTok' dengan mengklik 'Nanti saja'."""
    # Timeout 5000 -> 1500: pop-up ini kalau muncul, muncul segera. Di
    # layout desktop biasanya tidak muncul sama sekali, jadi versi lama
    # membayar 5 detik penuh untuk sesuatu yang tidak ada.
    clicked = await click_first(page, APP_POPUP_SELECTORS, timeout=1500)
    if clicked:
        print("[POPUP] Pop-up 'Tonton sekarang' ditutup (klik Nanti saja).")
    else:
        print("[POPUP] Tidak ada pop-up 'Tonton sekarang'.")
    return clicked


async def _open_desktop_page(browser: Browser, source_context: BrowserContext):
    """
    Buka context + page baru dengan viewport DESKTOP (bukan emulasi mobile).

    Ini diperlukan karena versi mobile web TikTok (yang dipakai untuk login,
    emulasi iPhone 12) hanya menampilkan halaman "buka di app" yang sangat
    disederhanakan - tidak ada menu "..." maupun opsi Report sama sekali di
    situ. Menu Report hanya muncul di layout desktop.

    Cookies dari `source_context` (context tempat login berhasil) dibawa ke
    context baru ini lewat storage_state, supaya tetap dalam keadaan login
    tanpa perlu login ulang.
    """
    storage = await source_context.storage_state()
    desktop_context = await browser.new_context(
        storage_state=storage,
        viewport={"width": 1280, "height": 800},
        locale="id-ID",
        timezone_id="Asia/Jakarta",
    )
    if BLOCK_HEAVY_RESOURCES:
        await block_heavy_resources(desktop_context, block_images=BLOCK_IMAGES)
    desktop_page = await desktop_context.new_page()
    return desktop_context, desktop_page


async def _goto_with_retry(page: Page, url: str, attempts: int = 3) -> bool:
    """
    Buka URL dengan retry berjenjang dan laporan status yang jelas.

    Versi lama memanggil page.goto() sekali tanpa penanganan apa pun.
    Akibatnya kegagalan sesaat - misalnya TikTok membalas 4xx karena
    membatasi permintaan - langsung muncul sebagai stack trace mentah
    `net::ERR_HTTP_RESPONSE_CODE_FAILURE` yang tidak menjelaskan apa pun.

    Catatan: `asyncio.sleep()` di sini adalah jeda backoff yang disengaja
    antar percobaan, bukan jeda buta menunggu UI.
    """
    delay = 2
    last_error = ""

    for i in range(1, attempts + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = response.status if response else 0
            if status and status >= 400:
                last_error = f"HTTP {status}"
                print(f"[REPORT] Percobaan {i}/{attempts}: TikTok membalas {status}.")
            else:
                return True
        except Exception as e:
            last_error = str(e).splitlines()[0]
            print(f"[REPORT] Percobaan {i}/{attempts} gagal: {last_error}")

        if i < attempts:
            print(f"[REPORT] Menunggu {delay}s sebelum mencoba lagi...")
            await asyncio.sleep(delay)
            delay *= 2

    print(f"[ERROR] Tidak bisa membuka halaman video. Penyebab terakhir: {last_error}")
    if "RESPONSE_CODE" in last_error or "HTTP 4" in last_error or "HTTP 5" in last_error:
        print("[ERROR] Status 4xx/5xx pada URL video umumnya berarti salah satu dari:")
        print("[ERROR]   - TikTok sedang membatasi permintaan dari sesi/IP ini")
        print("[ERROR]   - video sudah dihapus atau URL-nya salah")
        print("[ERROR] Kalau ini pembatasan, hentikan dulu - mengulang cepat memperburuk.")
    return False


async def _prepare_video_page(page: Page) -> bool:
    """
    Tunggu sampai halaman video benar-benar siap dipakai, sambil menangani
    hal-hal yang bisa menghalangi (pop-up app, captcha).

    Versi lama melakukan ini secara berurutan dengan biaya tetap:
        sleep(3) + popup wait 5000 + captcha wait 3000  = ~11 detik,
    dibayar penuh walaupun tidak ada pop-up maupun captcha.

    Sekarang ketiga kemungkinan di-race sekaligus. Kalau yang muncul duluan
    adalah menu "..." (kasus normal), fungsi ini selesai dalam ratusan
    milidetik. Kalau yang muncul penghalang, ia ditangani lalu race diulang.

    Return True kalau menu "..." siap diklik.
    """
    # Urutan penting: MORE_MENU di indeks 0 supaya ia menang kalau menu
    # sudah siap, walaupun elemen lain kebetulan juga ada di halaman.
    selectors = [MORE_MENU_SELECTOR, CAPTCHA_SELECTOR] + APP_POPUP_SELECTORS + LOGIN_WALL_SELECTORS
    popup_start = 2
    wall_start = popup_start + len(APP_POPUP_SELECTORS)

    for attempt in range(3):
        outcome = await first_match(page, selectors, timeout=SLOW_TIMEOUT)

        if outcome is None:
            print("[REPORT] Halaman video tidak kunjung siap.")
            return False
        if outcome == 0:
            return True
        if outcome == 1:
            print("[REPORT] Captcha muncul sebelum menu terbuka.")
            await _maybe_solve_captcha(page)
            continue
        if outcome >= wall_start:
            raise StaleSessionError(
                "Halaman video menampilkan tombol 'Log masuk' - sesi tidak "
                "dikenali di halaman ini walaupun cek sesi di homepage lolos."
            )

        await close_tiktok_app_popup(page)

    return await first_match(page, [MORE_MENU_SELECTOR], timeout=FAST_TIMEOUT) is not None


async def _pick_reason(page: Page, label: str, pattern, fallback_index: int, previous_signature):
    """
    Pilih satu opsi di dialog report: coba cocokkan teks (dwibahasa), lalu
    fallback ke posisi urutan, lalu fallback manual.

    Perilaku pemilihannya sama persis dengan versi lama. Yang berubah cuma
    cara menunggunya: `asyncio.sleep(1)` setelah klik diganti deteksi
    pergantian panel yang sebenarnya (lihat perf.wait_panel_change).

    Return (berhasil, signature_panel_baru).
    """
    labels = page.locator(REASON_LABEL_SELECTOR)

    clicked = False
    try:
        await labels.filter(has_text=pattern).first.click(timeout=FAST_TIMEOUT)
        clicked = True
    except Exception:
        pass

    if not clicked:
        try:
            await labels.nth(fallback_index).click(timeout=FAST_TIMEOUT)
            clicked = True
        except Exception as e:
            print(f"[ERROR] Gagal memilih '{label}' (teks & posisi gagal): {e}")

    if clicked:
        print(f"[REPORT] '{label}' dipilih.")
    else:
        print(f"[INFO] Pilih '{label}' secara manual di jendela browser.")
        input("Setelah dipilih, tekan Enter untuk lanjut...")

    # Daftar kategori dan daftar sub-alasan memakai selector yang SAMA,
    # jadi "tunggu selector muncul" akan langsung lolos oleh daftar lama.
    # Yang dipakai di sini: tunggu isi panelnya benar-benar berganti.
    await wait_panel_change(page, REASON_LABEL_SELECTOR, previous_signature, timeout=FAST_TIMEOUT + 2000)
    new_signature = await panel_signature(page, REASON_LABEL_SELECTOR)
    return clicked, new_signature


async def report_video(page: Page, video_url: str) -> bool:
    """
    Fungsi utama untuk melakukan report video.
    Diharapkan page sudah dalam keadaan login.

    Return True kalau laporan terkonfirmasi terkirim.
    """
    sw = Stopwatch()
    print(f"\n[REPORT] Membuka video target: {video_url}")
    # "networkidle" dihindari: TikTok terus kirim request background
    # (ads/analytics/preload) yang nyaris tidak pernah benar-benar hening,
    # jadi wait_until="networkidle" sering timeout 30 detik padahal
    # halamannya sendiri sudah siap dipakai.
    if not await _goto_with_retry(page, video_url):
        return False

    if not await _prepare_video_page(page):
        print("[ERROR] Menu '...' tidak pernah muncul. Halaman mungkin tidak termuat benar.")
        return False
    sw.lap("halaman video siap")

    # Buka menu "..." (titik tiga) pada video.
    # CATATAN: opsi "Report" TIDAK ada di panel "Bagikan" pada UI TikTok
    # saat ini - Report hanya bisa diakses lewat menu titik-tiga ini.
    if not await click_first(page, [MORE_MENU_SELECTOR], timeout=FAST_TIMEOUT):
        print("[ERROR] Gagal membuka menu '...'.")
        return False
    print("[REPORT] Menu '...' diklik.")

    # Pengganti `asyncio.sleep(1)`: tunggu item Report benar-benar dirender.
    if not await click_first(page, [REPORT_ITEM_SELECTOR], timeout=FAST_TIMEOUT):
        print("[ERROR] Gagal menemukan opsi 'Report'.")
        return False
    print("[REPORT] Opsi 'Report' diklik.")

    # Dialog report isinya di-fetch, jadi pakai timeout jaringan.
    if await first_match(page, [REASON_LABEL_SELECTOR], timeout=SLOW_TIMEOUT) is None:
        print("[ERROR] Daftar alasan report tidak muncul.")
        return False
    sw.lap("dialog report terbuka")

    signature = await panel_signature(page, REASON_LABEL_SELECTOR)

    # Pilih kategori "Misinformation" - konten AI/deepfake ada di bawah
    # kategori ini, bukan sebagai kategori tersendiri di level pertama.
    #
    # CATATAN BAHASA: teks kategori bisa muncul dalam Bahasa Indonesia atau
    # Inggris tergantung akun/sesi. Karena itu dicoba dulu lewat teks
    # (dwibahasa, best-effort), lalu fallback ke POSISI urutan kategori
    # (index ke-8 dari atas = "Misinformation" pada urutan kebijakan
    # standar TikTok, yang biasanya tetap sama walau bahasa UI beda).
    _, signature = await _pick_reason(
        page,
        "Kategori Misinformation",
        re.compile(r"misinformation|informasi.*(salah|keliru)", re.I),
        fallback_index=7,
        previous_signature=signature,
    )

    # Sub-alasan spesifik untuk konten AI/deepfake/manipulasi.
    await _pick_reason(
        page,
        "Alasan Deepfakes/synthetic media",
        re.compile(r"deepfake|synthetic|manipulated media|sintetis|dimanipulasi", re.I),
        fallback_index=2,
        previous_signature=signature,
    )

    # Beberapa alur report TikTok menampilkan tombol konfirmasi/submit
    # tambahan setelah alasan dipilih - klik jika ada, tapi jangan
    # dianggap fatal kalau tidak ditemukan (mungkin sudah otomatis terkirim
    # begitu alasan terakhir dipilih).
    submitted = await click_first(
        page,
        [
            'button:has-text("Submit")',
            'button:has-text("Kirim")',
            'button:has-text("Report"):not([data-e2e])',
        ],
        timeout=FAST_TIMEOUT,
    )
    if submitted:
        print("[REPORT] Tombol submit diklik.")
    else:
        print("[INFO] Tidak ada tombol submit tambahan terdeteksi.")

    # Konfirmasi nyata, menggantikan "cek manual di browser" versi lama.
    confirmed = await first_match(page, REPORT_DONE_SELECTORS, timeout=SLOW_TIMEOUT) is not None
    sw.lap("submit + konfirmasi")

    if confirmed:
        print("[REPORT] Laporan terkonfirmasi terkirim.")
    else:
        print("[WARNING] Tidak menemukan konfirmasi 'terima kasih atas laporan'.")
        print("[WARNING] Laporan mungkin belum terkirim - periksa jendela browser.")

    print(f"[TIME] Total alur report: {sw.total():.2f}s")
    return confirmed


# ===== FUNGSI YANG DIPANGGIL OLEH main.py =====
async def main_report(page: Page, browser: Browser, context: BrowserContext):
    """
    Kompatibilitas untuk pemanggil lama: menerima context MOBILE hasil
    login, memindahkan cookies-nya ke context desktop, lalu report.

    main.py versi baru tidak lagi lewat sini - ia langsung membuat context
    desktop sejak awal supaya tidak perlu membuat dua context saat sesi
    tersimpan masih valid. Fungsi ini dipertahankan supaya script/pemanggil
    lain yang sudah ada tidak rusak.
    """
    desktop_context, desktop_page = await _open_desktop_page(browser, context)

    # Context mobile (dipakai untuk login) sudah tidak diperlukan lagi
    # setelah cookies-nya dipindah ke context desktop di atas.
    try:
        await context.close()
    except Exception:
        pass

    try:
        return await report_video(desktop_page, TARGET_VIDEO_URL)
    finally:
        await desktop_context.close()


# ===== STANDALONE (jika report.py dijalankan langsung tanpa main.py) =====
async def _standalone_main():
    # Import lokal supaya main.py tidak perlu ikut memuat ulang tiktok_login
    # setiap kali (menghindari import melingkar report <-> main).
    from tiktok_login import ensure_logged_in

    async with async_playwright() as p:
        device = p.devices.get("iPhone 12") or {
            "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
            "viewport": {"width": 390, "height": 844},
            "device_scale_factor": 3,
            "is_mobile": True,
            "has_touch": True,
        }

        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )

        has_saved_session = os.path.exists(STORAGE_STATE_PATH)
        context = await browser.new_context(
            **device,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            storage_state=STORAGE_STATE_PATH if has_saved_session else None,
        )
        if BLOCK_HEAVY_RESOURCES:
            await block_heavy_resources(context, block_images=BLOCK_IMAGES)
        page = await context.new_page()

        print("\n[LOGIN] Memulai proses login (mode standalone)...")
        await ensure_logged_in(page)
        await context.storage_state(path=STORAGE_STATE_PATH)
        print(f"[LOGIN] Sesi disimpan ke {STORAGE_STATE_PATH}")

        print("\n[LOGIN] Login selesai. Melakukan report...")
        desktop_context, desktop_page = await _open_desktop_page(browser, context)

        try:
            await context.close()
        except Exception:
            pass

        from tiktok_login import logout_tiktok, clear_session_file

        ok = False
        session_stale = False
        try:
            ok = await report_video(desktop_page, TARGET_VIDEO_URL)
        except StaleSessionError as e:
            session_stale = True
            print(f"[ERROR] {e}")
        except Exception as e:
            print(f"[ERROR] Gagal menjalankan report: {e}")
        finally:
            if session_stale and AUTO_CLEAR_STALE_SESSION:
                clear_session_file("sesi ditolak halaman video")
                print("[INFO] Run berikutnya akan login dari nol.")
            elif LOGOUT_AFTER_REPORT:
                try:
                    await logout_tiktok(desktop_page)
                except Exception as e:
                    print(f"[LOGOUT] Gagal: {e}")
                clear_session_file("logout")
            await desktop_context.close()

        print(f"\n[{'SUCCESS' if ok else 'SELESAI'}] Proses selesai.")
        if PAUSE_AT_END:
            input("Tekan Enter untuk menutup browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(_standalone_main())
