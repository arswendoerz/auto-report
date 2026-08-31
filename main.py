import asyncio
import os
import sys

from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

from config import (
    STORAGE_STATE_PATH,
    TARGET_VIDEO_URL,
    HEADLESS,
    BLOCK_HEAVY_RESOURCES,
    BLOCK_IMAGES,
    PAUSE_AT_END,
    LOGOUT_AFTER_REPORT,
    AUTO_CLEAR_STALE_SESSION,
)
from perf import Stopwatch, block_heavy_resources
from tiktok_login import login_tiktok, is_logged_in, logout_tiktok, clear_session_file
import report

# Flag peluncuran.
#   - --disable-blink-features=AutomationControlled: sudah ada sejak versi
#     lama, dipertahankan apa adanya.
#   - Sisanya murni performa: mematikan throttling timer/render saat jendela
#     tidak fokus (kalau tidak, Chromium memperlambat halaman di background
#     dan polling jadi terasa lambat), plus mempercepat startup profil baru.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]

MOBILE_FALLBACK = {
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 3,
    "is_mobile": True,
    "has_touch": True,
}


async def _new_context(browser, storage_state=None, device=None):
    options = dict(device or {})
    options.update(locale="id-ID", timezone_id="Asia/Jakarta")
    if device is None:
        options["viewport"] = {"width": 1280, "height": 800}
    if storage_state:
        options["storage_state"] = storage_state

    context = await browser.new_context(**options)
    if BLOCK_HEAVY_RESOURCES:
        await block_heavy_resources(context, block_images=BLOCK_IMAGES)
    return context


async def main():
    sw = Stopwatch()
    ok = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=LAUNCH_ARGS)
        sw.lap("luncurkan browser")

        # PERUBAHAN ALUR (ini penghematan terbesar di run berulang):
        #
        # Versi lama SELALU membuat context mobile (emulasi iPhone 12) lebih
        # dulu, memuat tiktok.com di situ untuk cek sesi, lalu - karena menu
        # Report cuma ada di layout desktop - membuat context desktop KEDUA
        # dan menyalin cookies ke sana. Jadi setiap run membayar dua kali
        # pembuatan context dan dua kali muat halaman, padahal context mobile
        # itu hanya benar-benar dibutuhkan kalau harus login dari nol.
        #
        # Sekarang: langsung buka context desktop dan cek sesi di situ.
        # Kalau sesi masih valid (kasus paling umum), context mobile tidak
        # pernah dibuat sama sekali dan alur langsung lanjut ke report.
        has_saved_session = os.path.exists(STORAGE_STATE_PATH)
        context = await _new_context(
            browser, storage_state=STORAGE_STATE_PATH if has_saved_session else None
        )
        page = await context.new_page()

        logged_in = await is_logged_in(page) if has_saved_session else False
        sw.lap("cek sesi tersimpan")

        if logged_in:
            print("[LOGIN] Sesi tersimpan masih valid, login dilewati.")
        else:
            print("[LOGIN] Sesi tidak ada/sudah tidak valid, melakukan login penuh...")
            # Login tetap dijalankan di context mobile, sama seperti versi
            # lama - selector di tiktok_login.py disusun untuk layout itu.
            device = p.devices.get("iPhone 12") or MOBILE_FALLBACK
            mobile_context = await _new_context(browser, device=device)
            mobile_page = await mobile_context.new_page()

            await login_tiktok(mobile_page)
            await mobile_context.storage_state(path=STORAGE_STATE_PATH)
            print(f"[LOGIN] Sesi disimpan ke {STORAGE_STATE_PATH}")
            await mobile_context.close()

            # Buat ulang context desktop dengan cookies yang baru.
            await context.close()
            context = await _new_context(browser, storage_state=STORAGE_STATE_PATH)
            page = await context.new_page()
            sw.lap("login penuh")

        print("\n[MAIN] Menjalankan report...")
        session_stale = False
        try:
            ok = await report.report_video(page, TARGET_VIDEO_URL)
        except report.StaleSessionError as e:
            session_stale = True
            print(f"[ERROR] {e}")
            print("[ERROR] Menu '...' tidak akan pernah muncul dengan sesi ini.")
        except Exception as e:
            print(f"[ERROR] Gagal menjalankan report: {e}")
            print("[INFO] Anda bisa menjalankan report.py secara manual setelah login.")
        finally:
            if session_stale and AUTO_CLEAR_STALE_SESSION:
                # Hanya di cabang ini sesi dibuang otomatis - bukan tiap run.
                clear_session_file("sesi ditolak halaman video")
                print("[INFO] Run berikutnya akan login dari nol.")
                print("[INFO] Beri jeda dulu kalau TikTok sedang membatasi akun ini.")
            elif LOGOUT_AFTER_REPORT:
                try:
                    await logout_tiktok(page)
                except Exception as e:
                    print(f"[LOGOUT] Gagal: {e}")

                # Sesi sudah tidak berlaku setelah logout. File sesinya
                # dibuang supaya run berikutnya tidak mencoba memakai
                # cookies mati dan berakhir di layar "lanjutkan sebagai...".
                clear_session_file("logout")
            elif session_stale:
                print("[INFO] AUTO_CLEAR_STALE_SESSION mati - file sesi dibiarkan.")
                print(f"[INFO] Hapus {STORAGE_STATE_PATH} manual lalu jalankan lagi.")
            else:
                # Simpan sesi terbaru supaya run berikutnya bisa reuse dan
                # melewati login sepenuhnya.
                try:
                    await context.storage_state(path=STORAGE_STATE_PATH)
                except Exception:
                    pass

            print(f"\n[{'SUCCESS' if ok else 'SELESAI'}] Total waktu: {sw.total():.2f}s")
            if PAUSE_AT_END:
                input("Tekan Enter untuk menutup browser...")
            await browser.close()

    return ok


if __name__ == "__main__":
    asyncio.run(main())
