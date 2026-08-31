"""Selector UI TikTok yang dipakai lebih dari satu modul.

Nama file sengaja BUKAN `selectors.py`: nama itu akan menimpa modul bawaan
Python `selectors` yang dipakai asyncio, dan seluruh program bisa rusak.
"""

# Tombol "Log masuk". Dipakai dengan dua arti berbeda:
#   - tiktok_login : penanda sesi sudah berakhir (verifikasi setelah logout)
#   - report       : tembok login di halaman video (sesi tidak dikenali)
LOGIN_BUTTON_SELECTORS = [
    '[data-e2e="top-login-button"]',
    'button:has-text("Log masuk")',
    'button:has-text("Log in")',
]

# Pop-up "buka video ini di aplikasi TikTok".
APP_POPUP_SELECTORS = [
    '[data-e2e="bottom-cta-cancel-btn"]',
    "text=/Nanti saja/i",
    "text=/Not now/i",
]

# captcha_solver.py menyimpan salinannya sendiri dan sengaja tidak diubah,
# jadi kalau selector ini berubah, ubah juga di sana.
CAPTCHA_SELECTOR = '.captcha-verify-img-slide, .captcha-verify, div[class*="captcha"]'
