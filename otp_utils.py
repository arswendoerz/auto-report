"""
otp_utils.py - Ambil kode OTP TikTok dari inbox Gmail via IMAP.

Sebelumnya logika ini diduplikasi persis di main.py, report.py, dan
debug_otp.py. Sekarang disatukan di sini supaya perbaikan pola/selector
cukup dilakukan di satu tempat.

CATATAN PERFORMA & PERBAIKAN (dibanding versi sebelumnya):

1. PERBAIKAN BUG UTAMA - "Kode verifikasi kedaluwarsa atau salah".
   Versi lama mengambil email TikTok TERBARU YANG ADA saat polling
   dimulai. Masalahnya, email OTP yang baru butuh beberapa detik untuk
   sampai, sementara inbox biasanya masih menyimpan email OTP dari
   permintaan SEBELUMNYA. Jadi yang terbaca adalah kode lama - yang sudah
   otomatis kedaluwarsa begitu TikTok menerbitkan kode baru.
   Parameter `last_known_otp` sebenarnya sudah disediakan untuk mencegah
   ini, tapi pemanggil di tiktok_login.py tidak pernah mengisinya.

   Sekarang dipakai GARIS BATAS berbasis UID: sebelum kode diminta,
   `latest_tiktok_uid()` mencatat UID email TikTok paling baru. Polling
   kemudian HANYA menerima email dengan UID lebih besar dari itu, plus
   pemeriksaan umur email (default maksimal 10 menit).

2. Memakai perintah UID (`uid('search')`/`uid('fetch')`), bukan sequence
   number seperti versi lama. Sequence number bergeser kalau ada email
   dihapus/dipindah di tengah proses, sehingga "email ke-N" bisa menunjuk
   pesan yang berbeda antar polling. UID tidak pernah berubah.

3. Koneksi IMAP di-reuse. Versi lama melakukan IMAP4_SSL() + login() BARU
   setiap kali polling. Satu handshake TLS + login Gmail memakan ~1-2
   detik, dikali 6 percobaan = ~10 detik hangus hanya untuk connect ulang.

4. Email ditelusuri dari yang TERBARU dan berhenti di email pertama yang
   mengandung OTP. Versi lama selalu mengunduh 5 email penuh (RFC822)
   plus 5 query INTERNALDATE terpisah = 10 round-trip tiap polling.

5. PERBAIKAN BUG: versi lama mengurutkan kandidat dengan
   `candidates.sort(key=lambda x: x[0])` di mana x[0] adalah string mentah
   seperti `1 (INTERNALDATE "31-Aug-2026 12:48:00 +0700")`. Itu urutan
   LEKSIKOGRAFIS, bukan kronologis - "01-Sep" dianggap lebih tua daripada
   "31-Aug". Sekarang urutan diambil dari UID yang memang kronologis.

6. Interval polling 10 detik -> 3 detik (jumlah percobaan dinaikkan supaya
   total jendela tunggu tetap ~60 detik).
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

# Umur maksimal email OTP yang masih mau diterima (detik). Kode TikTok
# sendiri hanya berlaku beberapa menit, jadi email yang lebih tua dari ini
# sudah pasti tidak berguna dan lebih baik dilewati daripada diisikan.
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
    """
    Koneksi IMAP yang bisa dipakai berulang kali.

    `select("inbox")` dipanggil ulang tiap kali dipakai supaya email yang
    baru masuk setelah koneksi terbuka tetap terlihat oleh SEARCH.
    Kalau koneksi mati di tengah jalan, ia menyambung ulang sendiri.
    """

    def __init__(self):
        self._mail = None

    def _connect(self):
        mail = imaplib.IMAP4_SSL(EMAIL_IMAP_SERVER, EMAIL_IMAP_PORT)
        mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
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
    """Cari UID semua email dari TikTok. UID stabil, tidak bergeser."""
    status, data = mail.uid("search", None, '(FROM "tiktok")')
    if status != "OK" or not data or not data[0]:
        return []
    return [int(x) for x in data[0].split()]


def _message_age_seconds(mail, uid):
    """Umur email dalam detik, atau (None, "") kalau gagal dibaca."""
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
    """
    UID email TikTok terbaru SAAT INI. Dipakai sebagai garis batas: dicatat
    tepat sebelum kode OTP diminta, supaya email OTP lama tidak ikut
    terbaca oleh polling.

    Return 0 kalau inbox kosong atau IMAP gagal diakses. Nilai 0 berarti
    "tidak ada garis batas", dan polling akan memberi peringatan.
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
    """
    Cari email OTP terbaru yang UID-nya di atas `after_uid`.
    Return dict {otp, uid, subject, date, age} atau None.
    """
    mail = session.mail()
    mail.select("inbox")

    uids = _uid_search(mail)
    if not uids:
        return None

    # UID naik seiring waktu, jadi urutan terbalik = terbaru duluan.
    for uid in sorted(uids, reverse=True)[:max_lookback]:
        if after_uid and uid <= after_uid:
            # Sudah sampai ke email yang sudah ada SEBELUM kode diminta.
            # Semua sisanya lebih tua lagi, jadi tidak perlu dilanjutkan.
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


def fetch_latest_otp(max_lookback: int = 5, session: ImapSession = None, after_uid: int = 0,
                     max_age_seconds: int = None):
    """
    Ambil satu kali snapshot OTP terbaru dari email TikTok di inbox.
    Return (otp, date_str) atau (None, None) jika tidak ditemukan.

    `session` opsional: kalau diberikan, koneksi itu dipakai ulang (dipakai
    oleh wait_for_new_otp). Kalau tidak, koneksi sekali pakai dibuat dan
    ditutup lagi.

    `after_uid` opsional: hanya terima email yang lebih baru dari UID ini.
    """
    if session is not None:
        result = _fetch_with_session(session, max_lookback, after_uid, max_age_seconds)
    else:
        with ImapSession() as own_session:
            result = _fetch_with_session(own_session, max_lookback, after_uid, max_age_seconds)

    if not result:
        return None, None
    return result["otp"], result["date"]


def wait_for_new_otp(last_known_otp=None, max_attempts: int = 20, wait_seconds: int = 3,
                     after_uid: int = 0, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS):
    """
    Polling inbox sampai menemukan OTP dari email yang benar-benar BARU.

    `after_uid` adalah garis batasnya - ambil dari latest_tiktok_uid() yang
    dipanggil SEBELUM kode diminta ke TikTok. Tanpa ini, fungsi akan
    menerima OTP dari permintaan sebelumnya dan TikTok akan menolaknya
    dengan "Kode verifikasi kedaluwarsa atau salah".

    CATATAN: fungsi ini SINKRON dan memakai time.sleep(). Jangan panggil
    langsung dari coroutine - pakai `await asyncio.to_thread(...)` seperti
    di tiktok_login.py, supaya event loop tidak ikut membeku.
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
