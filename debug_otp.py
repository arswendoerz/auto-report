"""
debug_otp.py - Alat bantu debug untuk memeriksa proses pengambilan OTP
dari inbox Gmail (tanpa membuka browser TikTok).
"""

import imaplib
import email
import re
import sys
from email.header import decode_header

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

from config import EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT, EMAIL_ACCOUNT, EMAIL_PASSWORD
from otp_utils import OTP_PATTERNS


def test_otp_extraction():
    print("=== DEBUG OTP GMAIL ===\n")
    try:
        print("[TEST] Menghubungi IMAP server...")
        mail = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        print("[TEST] ✅ Login IMAP berhasil!")

        mail.select("inbox")
        print("[TEST] Folder inbox dipilih.")

        print("\n[TEST] Mencoba filter: FROM \"tiktok\" SUBJECT \"kode 6 digit\"")
        status, messages = mail.search(None, '(FROM "tiktok" SUBJECT "kode 6-digit Anda")')
        if status != "OK" or not messages[0]:
            print("[TEST] Filter spesifik tidak ditemukan, coba filter FROM \"tiktok\"")
            status, messages = mail.search(None, '(FROM "tiktok")')
        if status != "OK" or not messages[0]:
            print("[TEST] Tidak ada email dari TikTok sama sekali.")
            mail.close()
            mail.logout()
            return

        email_ids = messages[0].split()
        print(f"[TEST] ✅ Ditemukan {len(email_ids)} email dari TikTok.")
        latest_email_id = email_ids[-1]
        print(f"[TEST] Mengambil email ID: {latest_email_id}")

        status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        if status != "OK":
            print("[TEST] Gagal mengambil isi email.")
            mail.close()
            mail.logout()
            return

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject, encoding = decode_header(msg.get("Subject", ""))[0]
        if isinstance(subject, bytes):
            subject = subject.decode(encoding or "utf-8", errors="ignore")
        print(f"\n[SUBJECT] {subject}")

        from_header = msg.get("From", "")
        print(f"[FROM] {from_header}")

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

        print("\n[BODY] (full content):")
        print("-" * 40)
        print(body[:1000])
        if len(body) > 1000:
            print("... (truncated)")
        print("-" * 40)

        match_subject = re.search(r'\b(\d{6})\b', subject)
        if match_subject:
            otp = match_subject.group(1)
            if otp != "000000":
                print(f"\n✅ OTP dari subject: {otp}")

        print("\n[OTP from BODY] Mencoba berbagai pola:")
        found_otp = None
        for pattern in OTP_PATTERNS:
            match = re.search(pattern, body, re.IGNORECASE)
            if match:
                otp = match.group(1)
                if otp != "000000":
                    print(f"  ✅ Pattern '{pattern}' -> {otp}")
                    if not found_otp:
                        found_otp = otp
                else:
                    print(f"  ⚠️ Pattern '{pattern}' -> {otp} (ignored, placeholder)")
            else:
                print(f"  ❌ Pattern '{pattern}' tidak cocok")

        all_six = re.findall(r'\b(\d{6})\b', body)
        print(f"\n[ALL 6-DIGIT NUMBERS] {all_six[:10]}...")
        valid_otps = [d for d in all_six if d != "000000"]
        if valid_otps:
            print(f"[VALID OTP] {valid_otps}")
            if not found_otp:
                found_otp = valid_otps[0]

        if found_otp:
            print(f"\n✅ OTP yang valid: {found_otp}")
        else:
            print("\n❌ Tidak ditemukan OTP yang valid.")

        mail.close()
        mail.logout()
        print("\n[TEST] Selesai.")

    except Exception as e:
        print(f"[ERROR] {e}")


if __name__ == "__main__":
    test_otp_extraction()
