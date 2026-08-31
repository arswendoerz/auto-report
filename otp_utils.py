"""
otp_utils.py - Ambil kode OTP TikTok dari inbox Gmail via IMAP.

Sebelumnya logika ini diduplikasi persis di main.py, report.py, dan
debug_otp.py. Sekarang disatukan di sini supaya perbaikan pola/selector
cukup dilakukan di satu tempat.
"""

import imaplib
import email
import re
import time
from email.header import decode_header

from config import EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT, EMAIL_ACCOUNT, EMAIL_PASSWORD

# Urutan pola dicoba dari yang paling spesifik ke paling umum.
OTP_PATTERNS = [
    r'kode\s*6\s*digit.*?(\d{6})',
    r'Kode\s*6\s*digit.*?(\d{6})',
    r'kode.*?(\d{6})',
    r'verification\s*code.*?(\d{6})',
    r'code.*?(\d{6})',
    r'(\d{6})',
]


def _extract_otp_from_message(raw_email: bytes):
    """Parse satu email mentah. Return (subject, body, otp_or_None)."""
    msg = email.message_from_bytes(raw_email)

    subject, encoding = decode_header(msg.get("Subject", ""))[0]
    if isinstance(subject, bytes):
        subject = subject.decode(encoding or "utf-8", errors="ignore")

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="ignore")
                break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="ignore")

    otp = None
    match_subject = re.search(r'\b(\d{6})\b', subject)
    if match_subject:
        otp = match_subject.group(1)

    if not otp or otp == "000000":
        for pattern in OTP_PATTERNS:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                otp = match.group(1)
                break

    if otp == "000000":
        otp = None

    return subject, body, otp


def fetch_latest_otp(max_lookback: int = 5):
    """
    Ambil satu kali snapshot OTP terbaru dari email TikTok di inbox.
    Return (otp, date_str) atau (None, None) jika tidak ditemukan.
    """
    mail = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT)
    try:
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, '(FROM "tiktok")')
        if status != "OK" or not messages[0]:
            return None, None

        email_ids = messages[0].split()
        latest_ids = email_ids[-max_lookback:] if len(email_ids) >= max_lookback else email_ids

        candidates = []
        for email_id in latest_ids:
            status, data = mail.fetch(email_id, "(INTERNALDATE)")
            if status != "OK":
                continue
            date_str = data[0].decode() if isinstance(data[0], bytes) else str(data[0])

            status, msg_data = mail.fetch(email_id, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]

            _, _, otp = _extract_otp_from_message(raw_email)
            if otp:
                candidates.append((date_str, otp))

        if not candidates:
            return None, None

        candidates.sort(key=lambda x: x[0])
        return candidates[-1][1], candidates[-1][0]
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()


def wait_for_new_otp(last_known_otp=None, max_attempts: int = 6, wait_seconds: int = 10):
    """
    Polling inbox sampai menemukan OTP yang berbeda dari last_known_otp,
    atau sampai max_attempts habis (default: ~60 detik total, sama seperti
    perilaku asli di main.py). Dipakai saat login TikTok butuh verifikasi.
    """
    print(f"[OTP] Mencari OTP terbaru... (last_known: {last_known_otp})")
    for attempt in range(1, max_attempts + 1):
        try:
            otp, date_str = fetch_latest_otp()

            if otp is None:
                print(f"[OTP] Percobaan {attempt}/{max_attempts}: belum ada OTP. Tunggu {wait_seconds} detik...")
                time.sleep(wait_seconds)
                continue

            if last_known_otp and otp == last_known_otp:
                print(f"[OTP] Percobaan {attempt}/{max_attempts}: OTP masih sama ({otp}). Tunggu {wait_seconds} detik...")
                time.sleep(wait_seconds)
                continue

            print(f"[OTP] OTP terbaru: {otp} (dari {date_str})")
            return otp
        except Exception as e:
            print(f"[OTP] Error percobaan {attempt}/{max_attempts}: {e}")
            time.sleep(wait_seconds)

    print("[OTP] Gagal mendapatkan OTP setelah percobaan maksimal.")
    return None
