"""
perf.py - Helper performa untuk otomasi Playwright.

Modul ini tidak menambah fitur apa pun. Isinya hanya pengganti dari tiga
pola yang membuat versi lama lambat:

1. `asyncio.sleep()` buta setelah hampir setiap aksi. Nilainya dipilih
   "aman" (1-3 detik) padahal UI biasanya siap dalam ratusan milidetik,
   dan tetap dibayar penuh walau halaman sebenarnya sudah siap.
   Penggantinya: menunggu KONDISI (elemen berikutnya benar-benar muncul).

2. Percobaan selector berurutan. `_try_click()` lama mencoba selector satu
   per satu, masing-masing dengan timeout 5 detik. Tiga selector yang
   tidak ada berarti 15 detik terbuang sebelum fallback sempat jalan.

3. `asyncio.gather()` di `_wait_for_any()` lama menunggu SEMUA probe
   selesai. Jadi walaupun selector pertama langsung ketemu dalam 50 ms,
   fungsinya tetap menunggu probe lain habis timeout - biayanya selalu
   sebesar `timeout` penuh, tidak peduli secepat apa hasilnya.

Semua helper di sini memakai polling non-blocking: tiap siklus, seluruh
selector dicek sekali lewat `is_visible()` (query sekali jalan, TIDAK
menunggu, biayanya ~1-3 ms per selector), lalu langsung return begitu ada
yang cocok. Urutan prioritas selector tetap dihormati karena pengecekan
di dalam satu siklus dilakukan berurutan dari indeks 0.
"""

import asyncio
import time

# Jeda antar siklus polling (detik). 0.12 s cukup responsif (UI terdeteksi
# dalam ~1 frame manusia) tapi cukup jarang supaya tidak membanjiri
# koneksi CDP dengan ribuan query.
POLL_INTERVAL = 0.12

# Resource yang diblokir demi kecepatan muat halaman.
# CATATAN: "image" sengaja TIDAK ada di sini. Gambar dipakai oleh tampilan
# captcha, jadi memblokirnya bisa membuat alur captcha rusak. Kalau memang
# mau, aktifkan lewat BLOCK_IMAGES di .env.
BLOCKED_RESOURCE_TYPES = {"media", "font"}

# Host telemetri/iklan yang tidak dipakai alur otomasi sama sekali.
# Daftar ini sengaja sempit dan eksplisit - tidak memakai pola samar
# seperti "report" atau "api" yang berisiko ikut memblokir request asli.
BLOCKED_URL_HINTS = (
    "mon.tiktokv.com",
    "mon-va.tiktokv.com",
    "log-va.tiktokv.com",
    "analytics.tiktok.com",
    "log.byteoversea.com",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "connect.facebook.net",
)


def _as_list(selectors):
    if isinstance(selectors, str):
        return [selectors]
    return list(selectors)


def _locator(page, selector, has_text=None):
    if has_text is not None:
        return page.locator(selector, has_text=has_text).first
    return page.locator(selector).first


async def _visible(page, selector, has_text=None) -> bool:
    """
    Cek sekali apakah selector terlihat. `is_visible()` bersifat langsung
    (bukan API menunggu), jadi biayanya cuma satu round-trip CDP.
    Exception apa pun (selector invalid, halaman sedang navigasi) dianggap
    "belum terlihat" supaya polling tidak pernah crash.
    """
    try:
        return await _locator(page, selector, has_text).is_visible()
    except Exception:
        return False


async def first_match(page, selectors, timeout: int = 5000, has_text=None):
    """
    Tunggu sampai salah satu dari `selectors` terlihat.
    Return INDEKS selector yang cocok, atau None kalau habis `timeout` (ms).

    Pengganti `_wait_for_any()` lama. Bedanya: langsung return begitu ada
    yang cocok, tidak lagi menunggu probe lain ikut timeout.
    """
    selectors = _as_list(selectors)
    deadline = time.monotonic() + timeout / 1000
    while True:
        for i, sel in enumerate(selectors):
            if await _visible(page, sel, has_text):
                return i
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(POLL_INTERVAL)


async def click_first(
    page, selectors, timeout: int = 5000, has_text=None, click_timeout: int = 3000
) -> bool:
    """
    Klik elemen pertama yang terlihat dari daftar `selectors`.
    Tidak pernah melempar exception - return True/False.

    Pengganti `_try_click()` lama. Bedanya: seluruh daftar selector disapu
    dalam satu siklus polling, jadi biaya totalnya dibatasi `timeout`
    SEKALI - bukan `timeout` dikalikan jumlah selector.
    """
    selectors = _as_list(selectors)
    deadline = time.monotonic() + timeout / 1000
    while True:
        for sel in selectors:
            try:
                locator = _locator(page, sel, has_text)
                if await locator.is_visible():
                    await locator.click(timeout=click_timeout)
                    return True
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL_INTERVAL)


async def wait_gone(page, selectors, timeout: int = 8000) -> bool:
    """Tunggu sampai SEMUA selector tidak lagi terlihat. Return True kalau tercapai."""
    selectors = _as_list(selectors)
    deadline = time.monotonic() + timeout / 1000
    while True:
        still_there = False
        for sel in selectors:
            if await _visible(page, sel):
                still_there = True
                break
        if not still_there:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL_INTERVAL)


async def panel_signature(page, selector):
    """
    Sidik jari isi sebuah panel: (jumlah elemen, teks elemen pertama).

    Dipakai untuk mendeteksi pergantian panel yang memakai selector SAMA.
    Kasus nyatanya ada di dialog report TikTok: daftar kategori dan daftar
    sub-alasan dua-duanya `label[data-e2e="report-card-reason"]`, jadi
    "tunggu selector muncul" akan langsung lolos oleh daftar yang LAMA.
    Versi lama menutupi ini dengan `asyncio.sleep(1)` buta.
    """
    try:
        locator = page.locator(selector)
        count = await locator.count()
        text = (await locator.first.inner_text()) if count else ""
        return count, text.strip()
    except Exception:
        return -1, ""


async def wait_panel_change(page, selector, previous, timeout: int = 5000) -> bool:
    """Tunggu sampai sidik jari panel berubah dari `previous`."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        current = await panel_signature(page, selector)
        if current[0] > 0 and current != previous:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL_INTERVAL)


async def block_heavy_resources(context, block_images: bool = False):
    """
    Blokir resource yang tidak dibutuhkan alur otomasi.

    Yang paling berpengaruh adalah `media`: halaman video TikTok mengunduh
    stream video berukuran megabyte yang sama sekali tidak dipakai oleh
    otomasi (yang dibutuhkan hanya DOM-nya). Memblokirnya memangkas waktu
    muat halaman video secara signifikan, terutama di koneksi lambat.
    """
    types = set(BLOCKED_RESOURCE_TYPES)
    if block_images:
        types.add("image")

    async def _handler(route):
        try:
            request = route.request
            if request.resource_type in types:
                await route.abort()
                return
            if any(hint in request.url for hint in BLOCKED_URL_HINTS):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            # Jangan pernah biarkan handler routing menggantung request.
            try:
                await route.continue_()
            except Exception:
                pass

    await context.route("**/*", _handler)


class Stopwatch:
    """Pencatat durasi per tahap, supaya efek optimasi terlihat angkanya."""

    def __init__(self):
        self._start = time.monotonic()
        self._last = self._start

    def lap(self, label: str) -> float:
        now = time.monotonic()
        print(f"[TIME] {label}: {now - self._last:.2f}s (total {now - self._start:.2f}s)")
        self._last = now
        return now - self._start

    def total(self) -> float:
        return time.monotonic() - self._start
