"""Alat debug: periksa pengambilan OTP dari inbox Gmail tanpa membuka browser."""

import re
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

from otp_utils import (
    OTP_PATTERNS,
    ImapSession,
    _extract_otp_from_message,
    _message_age_seconds,
    _uid_search,
)


def main(max_lookback: int = 5):
    print("=== DEBUG OTP GMAIL ===\n")
    try:
        with ImapSession() as session:
            mail = session.mail()
            print("[TEST] Login IMAP berhasil.")
            mail.select("inbox")

            uids = _uid_search(mail)
            if not uids:
                print("[TEST] Tidak ada email dari TikTok sama sekali.")
                return

            newest = sorted(uids, reverse=True)[:max_lookback]
            print(f"[TEST] {len(uids)} email TikTok ditemukan. Memeriksa {len(newest)} terbaru.\n")

            for uid in newest:
                status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
                if status != "OK" or not isinstance(msg_data[0], tuple):
                    print(f"UID {uid}: gagal diambil.")
                    continue

                raw = msg_data[0][1]
                subject, body, otp = _extract_otp_from_message(raw)
                age, _ = _message_age_seconds(mail, uid)
                age_str = f"{int(age)}s" if age is not None else "?"

                print(f"UID {uid} | umur {age_str}")
                print(f"  subject : {subject}")
                print(f"  OTP     : {otp or '(tidak ditemukan)'}")

                if otp is None:
                    for pattern in OTP_PATTERNS:
                        m = re.search(pattern, body, re.IGNORECASE)
                        print(f"    {'OK ' if m else '-  '} {pattern} -> {m.group(1) if m else ''}")
                    all_six = re.findall(r"\b\d{6}\b", body)[:10]
                    print(f"    semua angka 6 digit: {all_six}")
                print()

            print("[TEST] Selesai.")
            print("[TEST] Catatan: saat login sungguhan, hanya email dengan UID DI ATAS")
            print("[TEST] garis batas yang diterima - lihat latest_tiktok_uid() di otp_utils.py.")
    except Exception as e:
        print(f"[ERROR] {e}")


if __name__ == "__main__":
    main()
