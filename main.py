import asyncio
import os
import sys

from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

from config import STORAGE_STATE_PATH
from tiktok_login import ensure_logged_in
import report


async def main():
    async with async_playwright() as p:
        device = p.devices.get("iPhone 12") or {
            "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
            "viewport": {"width": 390, "height": 844},
            "device_scale_factor": 3,
            "is_mobile": True,
            "has_touch": True,
        }

        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # Muat sesi login sebelumnya kalau ada, supaya tidak perlu login
        # dari nol tiap kali (mengurangi frekuensi OTP/captcha).
        has_saved_session = os.path.exists(STORAGE_STATE_PATH)
        context = await browser.new_context(
            **device,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            storage_state=STORAGE_STATE_PATH if has_saved_session else None,
        )
        page = await context.new_page()

        # ---------- LOGIN (dilewati otomatis kalau sesi tersimpan masih valid) ----------
        await ensure_logged_in(page)

        # Simpan sesi terbaru supaya run berikutnya bisa reuse.
        await context.storage_state(path=STORAGE_STATE_PATH)
        print(f"[LOGIN] Sesi disimpan ke {STORAGE_STATE_PATH}")

        # ---------- LOGIN SELESAI, LANJUT KE REPORT ----------
        print("\n[MAIN] Login selesai. Menjalankan report...")
        try:
            await report.main_report(page, browser, context)
        except Exception as e:
            print(f"[ERROR] Gagal menjalankan report: {e}")
            print("[INFO] Anda bisa menjalankan report.py secara manual setelah login.")
            input("Tekan Enter untuk menutup browser...")
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
