"""Entry point: pastikan sesi login siap, lalu jalankan alur report."""

import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

from config import (
    STORAGE_STATE_PATH,
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

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    # Tanpa tiga flag ini Chromium memperlambat halaman saat jendela tidak
    # fokus, sehingga polling elemen jadi ikut melambat.
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


# Bentuk link video TikTok yang diterima. Link profil (tanpa /video/) dan
# link non-TikTok ditolak, supaya salah tempel tidak terlanjur diproses.
TIKTOK_URL_PATTERNS = [
    re.compile(r"^https?://(www\.|m\.)?tiktok\.com/@[\w.\-]+/video/\d+", re.I),
    re.compile(r"^https?://(vt|vm)\.tiktok\.com/[\w\-]+", re.I),
    re.compile(r"^https?://(www\.)?tiktok\.com/t/[\w\-]+", re.I),
]


def _normalize_tiktok_url(raw: str):
    """Rapikan input mentah jadi URL valid, atau None kalau bukan link video."""
    url = raw.strip().strip('"\'<>')
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    for pattern in TIKTOK_URL_PATTERNS:
        if pattern.match(url):
            return url
    return None


def ask_target_video_url(max_attempts: int = 3):
    """Tanyakan link video yang akan dilaporkan. Return None kalau dibatalkan.

    Ditanyakan sebelum browser dijalankan, supaya salah ketik tidak membuang
    waktu meluncurkan Chromium dan memuat sesi lebih dulu.
    """
    print("=" * 62)
    print("Masukkan link video TikTok yang akan dilaporkan.")
    print("  contoh : https://www.tiktok.com/@nama/video/1234567890")
    print("  atau   : https://vt.tiktok.com/XXXXXXX/  (short link, otomatis diikuti)")
    print("=" * 62)

    for attempt in range(1, max_attempts + 1):
        try:
            raw = input("Link video: ")
        except (EOFError, KeyboardInterrupt):
            print("\n[INPUT] Dibatalkan.")
            return None

        url = _normalize_tiktok_url(raw)
        if url:
            print(f"[INPUT] Target: {url}\n")
            return url

        sisa = max_attempts - attempt
        if not raw.strip():
            print("[INPUT] Link tidak boleh kosong.")
        else:
            print("[INPUT] Bukan link video TikTok yang valid.")
            print("[INPUT] Link profil saja (tanpa /video/) tidak bisa dilaporkan.")
        if sisa:
            print(f"[INPUT] Sisa percobaan: {sisa}\n")

    print("[INPUT] Terlalu banyak input tidak valid. Dibatalkan.")
    return None


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
    # Ditanyakan lebih dulu: kalau dibatalkan, browser tidak perlu diluncurkan.
    target_video_url = ask_target_video_url()
    if not target_video_url:
        return False

    sw = Stopwatch()
    ok = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=LAUNCH_ARGS)
        sw.lap("luncurkan browser")

        # Context desktop dibuat lebih dulu dan sesi dicek di situ. Kalau sesi
        # masih valid (kasus paling umum), context mobile tidak pernah dibuat
        # sama sekali - menu report memang hanya ada di layout desktop.
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
            # Login dijalankan di context mobile - selector di tiktok_login.py
            # disusun untuk layout itu.
            device = p.devices.get("iPhone 12") or MOBILE_FALLBACK
            mobile_context = await _new_context(browser, device=device)
            mobile_page = await mobile_context.new_page()

            await login_tiktok(mobile_page)
            await mobile_context.storage_state(path=STORAGE_STATE_PATH)
            print(f"[LOGIN] Sesi disimpan ke {STORAGE_STATE_PATH}")
            await mobile_context.close()

            await context.close()
            context = await _new_context(browser, storage_state=STORAGE_STATE_PATH)
            page = await context.new_page()
            sw.lap("login penuh")

        print("\n[MAIN] Menjalankan report...")
        session_stale = False
        try:
            ok = await report.report_video(page, target_video_url)
        except report.StaleSessionError as e:
            session_stale = True
            print(f"[ERROR] {e}")
            print("[ERROR] Menu '...' tidak akan pernah muncul dengan sesi ini.")
        except Exception as e:
            print(f"[ERROR] Gagal menjalankan report: {e}")
            print("[INFO] Sesi login tetap tersimpan - jalankan ulang python main.py.")
        finally:
            if session_stale and AUTO_CLEAR_STALE_SESSION:
                clear_session_file("sesi ditolak halaman video")
                print("[INFO] Run berikutnya akan login dari nol.")
                print("[INFO] Beri jeda dulu kalau TikTok sedang membatasi akun ini.")
            elif LOGOUT_AFTER_REPORT:
                try:
                    await logout_tiktok(page)
                except Exception as e:
                    print(f"[LOGOUT] Gagal: {e}")
                clear_session_file("logout")
            elif session_stale:
                print("[INFO] AUTO_CLEAR_STALE_SESSION mati - file sesi dibiarkan.")
                print(f"[INFO] Hapus {STORAGE_STATE_PATH} manual lalu jalankan lagi.")
            else:
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
