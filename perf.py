"""Helper menunggu elemen berbasis kondisi + pemblokir resource berat."""

import asyncio
import time

POLL_INTERVAL = 0.12

# "image" sengaja tidak diblokir: tampilan captcha butuh gambar.
BLOCKED_RESOURCE_TYPES = {"media", "font"}

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
    try:
        return await _locator(page, selector, has_text).is_visible()
    except Exception:
        return False


async def first_match(page, selectors, timeout: int = 5000, has_text=None):
    """Return indeks selector pertama yang terlihat, atau None kalau habis timeout (ms).

    Semua selector disapu tiap siklus polling, jadi biaya totalnya dibatasi
    `timeout` sekali - bukan `timeout` dikali jumlah selector. Urutan daftar
    berlaku sebagai prioritas.
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
    """Klik elemen pertama yang terlihat. Tidak pernah melempar exception."""
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


async def panel_signature(page, selector):
    """Sidik jari isi panel: (jumlah elemen, teks elemen pertama)."""
    try:
        locator = page.locator(selector)
        count = await locator.count()
        text = (await locator.first.inner_text()) if count else ""
        return count, text.strip()
    except Exception:
        return -1, ""


async def wait_panel_change(page, selector, previous, timeout: int = 5000) -> bool:
    """Tunggu isi panel berganti dari `previous`.

    Dibutuhkan saat dua panel berbeda memakai selector yang SAMA - di dialog
    report TikTok, daftar kategori dan daftar sub-alasan dua-duanya
    `label[data-e2e="report-card-reason"]`, jadi "tunggu selector muncul"
    akan langsung lolos oleh daftar yang lama.
    """
    deadline = time.monotonic() + timeout / 1000
    while True:
        current = await panel_signature(page, selector)
        if current[0] > 0 and current != previous:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL_INTERVAL)


async def block_heavy_resources(context, block_images: bool = False):
    """Blokir stream video, font, dan host telemetri untuk mempercepat muat halaman."""
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
            try:
                await route.continue_()
            except Exception:
                pass

    await context.route("**/*", _handler)


class Stopwatch:
    """Pencatat durasi per tahap."""

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
