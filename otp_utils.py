"""Ambil kode OTP TikTok dari inbox Gmail via IMAP."""

import imaplib
import email
import re
import time
from email.header import decode_header

import config
# Dibaca lewat `config.NAMA` supaya kredensial yang diisi dari form gui.py
# (config.apply_credentials) ikut terbaca di sini.

# Dicoba dari yang paling spesifik ke paling umum.
OTP_PATTERNS = [
    r'kode\s*6\s*digit.*?(\d{6})',
    r'Kode\s*6\s*digit.*?(\d{6})',
    r'kode.*?(\d{6})',
    r'verification\s*code.*?(\d{6})',
    r'code.*?(\d{6})',
    r'(\d{6})',
]

DEFAULT_MAX_AGE_SECONDS = 600


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


class ImapSession:
    """Koneksi IMAP yang dipakai ulang antar polling, menyambung ulang bila putus."""

    def __init__(self):
        self._mail = None

    def _connect(self):
        mail = imaplib.IMAP4_SSL(config.EMAIL_IMAP_SERVER, config.EMAIL_IMAP_PORT)
        mail.login(config.EMAIL_ACCOUNT, config.EMAIL_PASSWORD)
        self._mail = mail
        return mail

    def mail(self):
        if self._mail is None:
            return self._connect()
        try:
            self._mail.noop()
            return self._mail
        except Exception:
            self.close()
            return self._connect()

    def close(self):
        if self._mail is None:
            return
        try:
            self._mail.close()
        except Exception:
            pass
        try:
            self._mail.logout()
        except Exception:
            pass
        self._mail = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _uid_search(mail):
    """UID semua email dari TikTok. UID dipakai (bukan sequence number) karena
    tidak bergeser saat ada email lain dihapus di tengah polling."""
    status, data = mail.uid("search", None, '(FROM "tiktok")')
    if status != "OK" or not data or not data[0]:
        return []
    return [int(x) for x in data[0].split()]


def _message_age_seconds(mail, uid):
    """Return (umur_detik_atau_None, date_str)."""
    try:
        status, data = mail.uid("fetch", str(uid), "(INTERNALDATE)")
        if status != "OK" or not data or not data[0]:
            return None, ""
        raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
        date_str = raw.decode(errors="ignore")
        stamp = imaplib.Internaldate2tuple(raw)
        if stamp is None:
            return None, date_str
        return time.time() - time.mktime(stamp), date_str
    except Exception:
        return None, ""


def latest_tiktok_uid(session: ImapSession = None) -> int:
    """UID email TikTok terbaru saat ini, atau 0 kalau gagal.

    Dipakai sebagai garis batas: dicatat SEBELUM kode diminta ke TikTok.
    """

    def _run(active):
        mail = active.mail()
        mail.select("inbox")
        uids = _uid_search(mail)
        return max(uids) if uids else 0

    try:
        if session is not None:
            return _run(session)
        with ImapSession() as own:
            return _run(own)
    except Exception as e:
        print(f"[OTP] Gagal membaca garis batas inbox: {e}")
        return 0


def _fetch_with_session(session, max_lookback, after_uid=0, max_age_seconds=None):
    """Email OTP terbaru dengan UID di atas `after_uid`.

    Return dict {otp, uid, subject, date, age} atau None.
    """
    mail = session.mail()
    mail.select("inbox")

    uids = _uid_search(mail)
    if not uids:
        return None

    for uid in sorted(uids, reverse=True)[:max_lookback]:
        if after_uid and uid <= after_uid:
            # Sudah sampai ke email yang ada sebelum kode diminta; sisanya
            # pasti lebih tua lagi.
            break

        status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
        if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            continue

        subject, _, otp = _extract_otp_from_message(msg_data[0][1])
        if not otp:
            continue

        age, date_str = _message_age_seconds(mail, uid)
        if max_age_seconds and age is not None and age > max_age_seconds:
            print(f"[OTP] Dilewati - email terlalu tua ({int(age)}s): {subject}")
            continue

        return {"otp": otp, "uid": uid, "subject": subject, "date": date_str, "age": age}

    return None


def wait_for_new_otp(last_known_otp=None, max_attempts: int = 20, wait_seconds: int = 3,
                     after_uid: int = 0, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS):
    """Polling inbox sampai ada OTP dari email yang benar-benar baru.

    `after_uid` WAJIB diisi dari latest_tiktok_uid() yang dipanggil sebelum kode
    diminta. Tanpa itu, email OTP dari permintaan sebelumnya ikut terbaca dan
    TikTok menolaknya dengan "Kode verifikasi kedaluwarsa atau salah".

    Fungsi ini sinkron dan memakai time.sleep() - panggil lewat
    `asyncio.to_thread(...)` supaya event loop tidak ikut membeku.
    """
    if after_uid:
        print(f"[OTP] Menunggu email OTP baru (setelah UID {after_uid})...")
    else:
        print("[OTP] PERINGATAN: tidak ada garis batas UID.")
        print("[OTP] Email OTP lama bisa ikut terbaca dan ditolak TikTok.")

    with ImapSession() as session:
        for attempt in range(1, max_attempts + 1):
            try:
                result = _fetch_with_session(session, 5, after_uid, max_age_seconds)

                if result is None:
                    print(f"[OTP] Percobaan {attempt}/{max_attempts}: email OTP baru belum masuk.")
                elif last_known_otp and result["otp"] == last_known_otp:
                    print(f"[OTP] Percobaan {attempt}/{max_attempts}: OTP masih sama ({result['otp']}).")
                else:
                    age = f"{int(result['age'])}s" if result["age"] is not None else "?"
                    print(f"[OTP] Dapat OTP {result['otp']} (umur {age}) dari: {result['subject']}")
                    return result["otp"]
            except Exception as e:
                print(f"[OTP] Error percobaan {attempt}/{max_attempts}: {e}")

            if attempt < max_attempts:
                time.sleep(wait_seconds)

    print("[OTP] Gagal mendapatkan OTP baru setelah percobaan maksimal.")
    return None
