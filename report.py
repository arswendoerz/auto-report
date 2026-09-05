"""Alur report video TikTok: buka video, pilih kategori & alasan, kirim laporan.

Dipanggil oleh main.py. Page yang dikirim harus sudah login dan berlayout desktop.
"""

import asyncio
import re
import sys

from playwright.async_api import Page

import config
from config import FAST_TIMEOUT, SLOW_TIMEOUT
from captcha_solver import check_and_solve_captcha
from perf import (
    Stopwatch,
    click_first,
    first_match,
    panel_signature,
    wait_panel_change,
)
from ui_selectors import APP_POPUP_SELECTORS, CAPTCHA_SELECTOR, LOGIN_BUTTON_SELECTORS

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')


MORE_MENU_SELECTOR = 'button[data-e2e="more-menu-icon"]'
REPORT_ITEM_SELECTOR = '[data-e2e="more-menu-popover_report"]'

# Daftar kategori DAN daftar sub-alasan memakai selector yang sama.
REASON_LABEL_SELECTOR = 'label[data-e2e="report-card-reason"]'

REPORT_DONE_SELECTORS = [
    "text=/Terima kasih atas laporan/i",
    "text=/Thanks for (your report|reporting)/i",
    "text=/Laporan.*(terkirim|diterima)/i",
    "text=/[Rr]eport (submitted|received)/i",
]


class ManualStepRequired(RuntimeError):
    """Alur report butuh campur tangan manusia, tapi mode UNATTENDED aktif.

    Dilempar supaya run ini dihitung gagal dan batch lanjut ke akun
    berikutnya, bukan menggantung menunggu jawaban yang tidak akan datang.
    """


class StaleSessionError(RuntimeError):
    """Halaman video menolak sesi yang dipakai.

    Dibedakan dari kegagalan biasa karena penanganannya beda: kegagalan lain
    layak dicoba ulang, sedangkan sesi yang ditolak tidak akan pernah berhasil
    berapa kali pun diulang - cookies-nya harus dibuang dan login diulang.
    """


async def _maybe_solve_captcha(page: Page) -> bool:
    """Probe cepat dulu; di sebagian besar run tidak ada captcha sama sekali."""
    if await first_match(page, [CAPTCHA_SELECTOR], timeout=600) is None:
        return False
    return await check_and_solve_captcha(page)


async def _close_app_popup(page: Page) -> bool:
    """Tutup pop-up 'Tonton video ini di TikTok'."""
    clicked = await click_first(page, APP_POPUP_SELECTORS, timeout=1500)
    if clicked:
        print("[POPUP] Pop-up 'Tonton sekarang' ditutup (klik Nanti saja).")
    else:
        print("[POPUP] Tidak ada pop-up 'Tonton sekarang'.")
    return clicked


async def _goto_with_retry(page: Page, url: str, attempts: int = 3) -> bool:
    """Buka URL dengan backoff dan laporan status HTTP yang jelas."""
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
    """Tunggu menu '...' siap, sambil menangani pop-up dan captcha.

    Raise StaleSessionError kalau halaman menampilkan tembok login.
    """
    # MORE_MENU di indeks 0 supaya ia menang kalau menu sudah siap.
    selectors = (
        [MORE_MENU_SELECTOR, CAPTCHA_SELECTOR] + APP_POPUP_SELECTORS + LOGIN_BUTTON_SELECTORS
    )
    wall_start = 2 + len(APP_POPUP_SELECTORS)

    for _ in range(3):
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

        await _close_app_popup(page)

    return await first_match(page, [MORE_MENU_SELECTOR], timeout=FAST_TIMEOUT) is not None


async def _pick_reason(page: Page, label: str, pattern, fallback_index: int, previous_signature):
    """Pilih opsi lewat teks, fallback ke posisi urutan, fallback manual.

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
        if config.manual_input("Setelah dipilih, tekan Enter untuk lanjut...") is None:
            # Tanpa opsi ini terpilih, sisa alur report tidak ada artinya.
            raise ManualStepRequired(f"opsi '{label}' tidak bisa dipilih otomatis")

    await wait_panel_change(page, REASON_LABEL_SELECTOR, previous_signature, timeout=FAST_TIMEOUT + 2000)
    return clicked, await panel_signature(page, REASON_LABEL_SELECTOR)


async def report_video(page: Page, video_url: str) -> bool:
    """Report satu video. Return True kalau laporan terkonfirmasi terkirim."""
    sw = Stopwatch()
    print(f"\n[REPORT] Membuka video target: {video_url}")
    if not await _goto_with_retry(page, video_url):
        return False

    if not await _prepare_video_page(page):
        print("[ERROR] Menu '...' tidak pernah muncul. Halaman mungkin tidak termuat benar.")
        return False
    sw.lap("halaman video siap")

    # Opsi "Report" tidak ada di panel "Bagikan" - hanya lewat menu titik-tiga.
    if not await click_first(page, [MORE_MENU_SELECTOR], timeout=FAST_TIMEOUT):
        print("[ERROR] Gagal membuka menu '...'.")
        return False
    print("[REPORT] Menu '...' diklik.")

    if not await click_first(page, [REPORT_ITEM_SELECTOR], timeout=FAST_TIMEOUT):
        print("[ERROR] Gagal menemukan opsi 'Report'.")
        return False
    print("[REPORT] Opsi 'Report' diklik.")

    if await first_match(page, [REASON_LABEL_SELECTOR], timeout=SLOW_TIMEOUT) is None:
        print("[ERROR] Daftar alasan report tidak muncul.")
        return False
    sw.lap("dialog report terbuka")

    signature = await panel_signature(page, REASON_LABEL_SELECTOR)

    # Konten AI/deepfake ada di bawah "Misinformation", bukan kategori sendiri.
    # fallback_index=7: urutan ke-8 pada daftar kebijakan standar TikTok, dipakai
    # kalau pencocokan teks gagal karena UI muncul dalam bahasa lain.
    _, signature = await _pick_reason(
        page,
        "Kategori Misinformation",
        re.compile(r"misinformation|informasi.*(salah|keliru)", re.I),
        fallback_index=7,
        previous_signature=signature,
    )

    await _pick_reason(
        page,
        "Alasan Deepfakes/synthetic media",
        re.compile(r"deepfake|synthetic|manipulated media|sintetis|dimanipulasi", re.I),
        fallback_index=2,
        previous_signature=signature,
    )

    # Tombol submit tidak selalu ada - sebagian alur langsung terkirim begitu
    # alasan terakhir dipilih.
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

    confirmed = await first_match(page, REPORT_DONE_SELECTORS, timeout=SLOW_TIMEOUT) is not None
    sw.lap("submit + konfirmasi")

    if confirmed:
        print("[REPORT] Laporan terkonfirmasi terkirim.")
    else:
        print("[WARNING] Tidak menemukan konfirmasi 'terima kasih atas laporan'.")
        print("[WARNING] Laporan mungkin belum terkirim - periksa jendela browser.")

    print(f"[TIME] Total alur report: {sw.total():.2f}s")
    return confirmed


if __name__ == "__main__":
    # report.py sekarang modul, bukan entry point. Pesan ini mencegah
    # `python report.py` berakhir diam tanpa melakukan apa pun.
    print("report.py adalah modul, bukan entry point.")
    print("Jalankan: python main.py")
