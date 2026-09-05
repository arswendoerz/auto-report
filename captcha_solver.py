"""
captcha_solver.py - Modul untuk mendeteksi dan menyelesaikan captcha slider TikTok
Menggunakan OpenCV untuk deteksi celah dan Playwright untuk simulasi drag.
"""

import asyncio
import cv2
import numpy as np
from playwright.async_api import Page
import os

import config

async def solve_slider_captcha(page: Page) -> bool:
    """
    Mencoba menyelesaikan captcha geser TikTok secara otomatis.
    Return True jika berhasil, False jika gagal (fallback ke manual).
    """
    try:
        # 1. Tunggu hingga elemen captcha muncul
        captcha_container = page.locator('.captcha-verify-img-slide, .captcha-verify, div[class*="captcha"]')
        await captcha_container.wait_for(state="visible", timeout=5000)
        print("[CAPTCHA] Terdeteksi slider puzzle. Mencoba menyelesaikan...")

        # 2. Ambil elemen gambar latar (yang ada celahnya)
        bg_img_element = page.locator('.captcha-verify-img-slide .bg-img, .captcha-verify-img-slide img:first-child')
        if await bg_img_element.count() == 0:
            bg_img_element = page.locator('.captcha-verify-img-slide')

        box = await bg_img_element.bounding_box()
        if not box:
            print("[CAPTCHA] Gagal mendapatkan posisi elemen.")
            return False

        # Ambil screenshot area captcha
        screenshot_bytes = await page.screenshot(clip=box)
        temp_path = "temp_captcha.png"
        with open(temp_path, "wb") as f:
            f.write(screenshot_bytes)

        # 3. Baca gambar dengan OpenCV
        img = cv2.imread(temp_path)
        if img is None:
            print("[CAPTCHA] Gagal membaca gambar.")
            return False

        # 4. Deteksi celah (gap) dengan edge detection
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)

        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        gap_contour = None
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            aspect_ratio = w / h if h != 0 else 0
            if 0.8 < aspect_ratio < 2.0 and w > 20 and h > 20:
                center_x, center_y = x + w//2, y + h//2
                if center_x > box['width'] * 0.2 and center_x < box['width'] * 0.8:
                    gap_contour = cnt
                    break

        if gap_contour is None:
            print("[CAPTCHA] Tidak dapat mendeteksi celah. Coba metode alternatif...")
            if contours:
                filtered = [c for c in contours if cv2.boundingRect(c)[0] > box['width']*0.2]
                if filtered:
                    gap_contour = max(filtered, key=cv2.contourArea)
                else:
                    return False
            else:
                return False

        # 5. Hitung jarak geser (dalam piksel)
        x, y, w, h = cv2.boundingRect(gap_contour)
        gap_center_x = x + w // 2
        distance = gap_center_x - 20  # offset awal slider
        if distance < 10:
            distance = 150  # fallback

        print(f"[CAPTCHA] Jarak geser yang dibutuhkan: {distance} px")

        # 6. Temukan elemen slider (tombol yang bisa digeser)
        slider = page.locator('.captcha-verify-img-slide .slide-img, .captcha-verify-img-slide .slide, .slide-img')
        await slider.wait_for(state="visible", timeout=5000)

        box_slider = await slider.bounding_box()
        if not box_slider:
            print("[CAPTCHA] Gagal mendapatkan posisi slider.")
            return False

        start_x = box_slider['x'] + box_slider['width'] // 2
        start_y = box_slider['y'] + box_slider['height'] // 2

        # 7. Lakukan drag dengan gerakan alami (bertahap)
        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(0.2)
        await page.mouse.down()
        await asyncio.sleep(0.1)

        steps = 4
        segment = distance / steps
        for i in range(steps):
            current_x = start_x + (segment * (i + 1)) + np.random.randint(-3, 3)
            current_y = start_y + np.random.randint(-2, 2)
            await page.mouse.move(current_x, current_y)
            await asyncio.sleep(0.1 + (np.random.random() * 0.05))

        await page.mouse.up()
        await asyncio.sleep(0.5)

        # 8. Tunggu apakah captcha hilang (berhasil)
        try:
            await captcha_container.wait_for(state="detached", timeout=5000)
            print("[CAPTCHA] ✅ Captcha berhasil diselesaikan!")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return True
        except:
            print("[CAPTCHA] ❌ Captcha mungkin gagal. Coba ulang atau manual.")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

    except Exception as e:
        print(f"[CAPTCHA] Error: {e}")
        return False


async def check_and_solve_captcha(page: Page) -> bool:
    """
    Cek apakah ada captcha, jika ada coba selesaikan otomatis.
    Jika gagal, minta input manual.
    """
    captcha_selector = '.captcha-verify-img-slide, .captcha-verify, div[class*="captcha"]'
    try:
        await page.wait_for_selector(captcha_selector, timeout=3000)
        print("[CAPTCHA] Captcha terdeteksi.")
        success = await solve_slider_captcha(page)
        if not success:
            print("[CAPTCHA] Gagal otomatis. Silakan selesaikan secara manual.")
            if config.manual_input("Selesaikan captcha manual, lalu tekan Enter...") is None:
                return False
            await page.wait_for_selector(captcha_selector, state="detached", timeout=30000)
            print("[CAPTCHA] Captcha selesai manual.")
        return True
    except:
        print("[CAPTCHA] Tidak ada captcha.")
        return False