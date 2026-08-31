import asyncio
import os
import re
import sys

from playwright.async_api import Page, Browser, BrowserContext, async_playwright

from config import TARGET_VIDEO_URL, STORAGE_STATE_PATH
from captcha_solver import check_and_solve_captcha

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')


async def close_tiktok_app_popup(page: Page):
    """Tutup pop-up 'Tonton video ini di TikTok' dengan mengklik 'Nanti saja'."""
    try:
        await page.wait_for_selector("text=Nanti saja", timeout=5000)
        await page.locator("text=Nanti saja").first.click()
        print("[POPUP] Pop-up 'Tonton sekarang' ditutup (klik Nanti saja).")
        await asyncio.sleep(1)
        return True
    except Exception:
        print("[POPUP] Tidak ada pop-up 'Tonton sekarang'.")
        return False


async def _open_desktop_page(browser: Browser, source_context: BrowserContext):
    """
    Buka context + page baru dengan viewport DESKTOP (bukan emulasi mobile).

    Ini diperlukan karena versi mobile web TikTok (yang dipakai untuk login,
    emulasi iPhone 12) hanya menampilkan halaman "buka di app" yang sangat
    disederhanakan — tidak ada menu "..." maupun opsi Report sama sekali di
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
    desktop_page = await desktop_context.new_page()
    return desktop_context, desktop_page


async def report_video(page: Page, video_url: str):
    """
    Fungsi utama untuk melakukan report video.
    Diharapkan page sudah dalam keadaan login.
    """
    print(f"\n[REPORT] Membuka video target: {video_url}")
    # "networkidle" dihindari: TikTok terus kirim request background
    # (ads/analytics/preload) yang nyaris tidak pernah benar-benar hening,
    # jadi wait_until="networkidle" sering timeout 30 detik padahal
    # halamannya sendiri sudah siap dipakai.
    await page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(3)

    # Tutup pop-up "Tonton sekarang"
    await close_tiktok_app_popup(page)

    # Cek captcha setelah buka video
    await check_and_solve_captcha(page)

    # Buka menu "..." (titik tiga) pada video.
    # CATATAN: opsi "Report" TIDAK ada di panel "Bagikan" pada UI TikTok
    # saat ini — Report hanya bisa diakses lewat menu titik-tiga ini.
    try:
        more_menu = page.locator('button[data-e2e="more-menu-icon"]').first
        await more_menu.click(timeout=5000)
        print("[REPORT] Menu '...' diklik.")
        await asyncio.sleep(1)
    except Exception as e:
        print(f"[ERROR] Gagal membuka menu '...': {e}")
        return

    # Klik opsi "Report" di menu tersebut
    try:
        report_item = page.locator('[data-e2e="more-menu-popover_report"]').first
        await report_item.click(timeout=5000)
        print("[REPORT] Opsi 'Report' diklik.")
        await asyncio.sleep(1)
    except Exception as e:
        print(f"[ERROR] Gagal menemukan opsi 'Report': {e}")
        return

    # Pilih kategori "Misinformation" — konten AI/deepfake ada di bawah
    # kategori ini, bukan sebagai kategori tersendiri di level pertama.
    #
    # CATATAN BAHASA: teks kategori bisa muncul dalam Bahasa Indonesia atau
    # Inggris tergantung akun/sesi. Karena itu dicoba dulu lewat teks
    # (dwibahasa, best-effort), lalu fallback ke POSISI urutan kategori
    # (index ke-8 dari atas = "Misinformation" pada urutan kebijakan
    # standar TikTok, yang biasanya tetap sama walau bahasa UI beda).
    # Kalau keduanya gagal, diminta pilih manual daripada berhenti total.
    reason_labels = page.locator('label[data-e2e="report-card-reason"]')
    try:
        await reason_labels.first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass

    category_clicked = False
    try:
        category = reason_labels.filter(
            has_text=re.compile(r"misinformation|informasi.*(salah|keliru)", re.I)
        ).first
        await category.click(timeout=5000)
        category_clicked = True
    except Exception:
        pass
    if not category_clicked:
        try:
            await reason_labels.nth(7).click(timeout=5000)  # urutan ke-8: Misinformation
            category_clicked = True
        except Exception as e:
            print(f"[ERROR] Gagal memilih kategori 'Misinformation' (teks & posisi gagal): {e}")

    if category_clicked:
        print("[REPORT] Kategori 'Misinformation' dipilih.")
        await asyncio.sleep(1)
    else:
        print("[INFO] Pilih kategori 'Misinformation' secara manual di jendela browser.")
        input("Setelah kategori dipilih, tekan Enter untuk lanjut...")

    # Pilih sub-alasan spesifik untuk konten AI/deepfake/manipulasi.
    # Sama seperti di atas: coba teks dulu, fallback ke posisi (index ke-3
    # dalam daftar sub-kategori Misinformation), lalu fallback manual.
    sub_reason_labels = page.locator('label[data-e2e="report-card-reason"]')
    try:
        await sub_reason_labels.first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass

    reason_clicked = False
    try:
        reason = sub_reason_labels.filter(
            has_text=re.compile(r"deepfake|synthetic|manipulated media|sintetis|dimanipulasi", re.I)
        ).first
        await reason.click(timeout=5000)
        reason_clicked = True
    except Exception:
        pass
    if not reason_clicked:
        try:
            await sub_reason_labels.nth(2).click(timeout=5000)  # urutan ke-3: Deepfakes/synthetic/manipulated media
            reason_clicked = True
        except Exception as e:
            print(f"[ERROR] Gagal memilih alasan AI/deepfake (teks & posisi gagal): {e}")

    if reason_clicked:
        print("[REPORT] Alasan 'Deepfakes/synthetic media' dipilih.")
        await asyncio.sleep(1)
    else:
        print("[INFO] Pilih alasan spesifik (AI/deepfake) secara manual di jendela browser.")
        input("Setelah alasan dipilih, tekan Enter untuk lanjut...")

    # Beberapa alur report TikTok menampilkan tombol konfirmasi/submit
    # tambahan setelah alasan dipilih — coba klik jika ada, tapi jangan
    # dianggap fatal kalau tidak ditemukan (mungkin sudah otomatis terkirim
    # begitu alasan terakhir dipilih).
    try:
        submit_button = page.locator(
            'button:has-text("Submit"), button:has-text("Kirim"), button:has-text("Report"):not([data-e2e])'
        ).first
        await submit_button.click(timeout=5000)
        print("[REPORT] Laporan berhasil dikirim!")
    except Exception as e:
        print(f"[INFO] Tidak ada tombol submit tambahan terdeteksi ({e}).")
        print("[INFO] Cek manual di browser apakah laporan sudah benar-benar terkirim.")


# ===== FUNGSI YANG DIPANGGIL OLEH main.py =====
async def main_report(page: Page, browser: Browser, context: BrowserContext):
    """
    Fungsi ini dipanggil oleh main.py setelah login.
    Melakukan report video lewat context desktop terpisah (lihat
    _open_desktop_page), memakai cookies login dari `context`.
    """
    desktop_context, desktop_page = await _open_desktop_page(browser, context)

    # Context mobile (dipakai untuk login) sudah tidak diperlukan lagi
    # setelah cookies-nya dipindah ke context desktop di atas. Ditutup di
    # sini supaya tidak ada dua jendela browser terbuka bersamaan yang
    # saling tumpang tindih/membingungkan.
    try:
        await context.close()
    except Exception:
        pass

    try:
        await report_video(desktop_page, TARGET_VIDEO_URL)
    finally:
        await desktop_context.close()

    print("\n[SUCCESS] Proses report selesai.")
    input("Tekan Enter untuk menutup browser...")
    await browser.close()


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
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        has_saved_session = os.path.exists(STORAGE_STATE_PATH)
        context = await browser.new_context(
            **device,
            locale="id-ID",
            timezone_id="Asia/Jakarta",
            storage_state=STORAGE_STATE_PATH if has_saved_session else None,
        )
        page = await context.new_page()

        print("\n[LOGIN] Memulai proses login (mode standalone)...")
        await ensure_logged_in(page)
        await context.storage_state(path=STORAGE_STATE_PATH)
        print(f"[LOGIN] Sesi disimpan ke {STORAGE_STATE_PATH}")

        print("\n[LOGIN] Login selesai. Melakukan report...")
        desktop_context, desktop_page = await _open_desktop_page(browser, context)

        # Tutup context mobile (sudah tidak dipakai lagi) supaya tidak ada
        # dua jendela browser terbuka bersamaan.
        try:
            await context.close()
        except Exception:
            pass

        try:
            await report_video(desktop_page, TARGET_VIDEO_URL)
        finally:
            await desktop_context.close()

        print("\n[SUCCESS] Proses selesai.")
        input("Tekan Enter untuk menutup browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(_standalone_main())
