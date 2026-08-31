"""Tempelkan jendela Chromium milik Playwright ke dalam frame Tkinter (Windows).

Dipakai gui.py supaya browser tampil di panel kanan, bukan sebagai jendela
lepas. Caranya jendela Chromium dicari lewat judul penanda, lalu di-`SetParent`
ke HWND frame Tk dan dekorasinya dilepas.

Semua fungsi di sini defensif: kalau apa pun gagal (bukan Windows, jendela
tidak ketemu, API menolak) mereka mengembalikan None/False dan pemanggil
membiarkan browser jalan sebagai jendela terpisah.
"""

import ctypes
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# Kelas jendela Chromium di Windows. Angka di belakangnya BUKAN konstan:
# Chromium menaikkannya per jendela dalam satu proses, jadi jendela pertama
# muncul sebagai Chrome_WidgetWin_1 dan jendela context berikutnya
# Chrome_WidgetWin_2, dst. Karena itu yang dicocokkan hanya prefiksnya.
#
# Prefiks ini juga dipakai semua aplikasi Electron (Spotify, Discord, VS Code)
# dan Chrome biasa. Jadi kelas SAJA tidak cukup untuk mengenali jendela milik
# browser yang kita luncurkan - selalu disaring lagi lewat judul penanda dan
# PID prosesnya.
CHROMIUM_WINDOW_CLASS_PREFIX = "Chrome_WidgetWin_"

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CLIPSIBLINGS = 0x04000000
WS_CLIPCHILDREN = 0x02000000
WS_POPUP = 0x80000000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_CAPTION = WS_BORDER | WS_DLGFRAME
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_OVERLAPPEDWINDOW = 0x00CF0000

WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_APPWINDOW = 0x00040000

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

SW_HIDE = 0
SW_SHOWNA = 8

_DECORATION_STYLES = (
    WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
)
_DECORATION_EX_STYLES = (
    WS_EX_DLGMODALFRAME | WS_EX_CLIENTEDGE | WS_EX_STATICEDGE | WS_EX_WINDOWEDGE | WS_EX_APPWINDOW
)

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    _user32.SetParent.restype = wintypes.HWND
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = wintypes.LONG
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    _user32.SetWindowLongW.restype = wintypes.LONG
    _user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.MoveWindow.argtypes = [
        wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL
    ]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
else:  # pragma: no cover - proyek ini dipakai di Windows
    _user32 = None


def _signed32(value: int) -> int:
    """LONG di Win32 bertanda; nilai style seperti WS_POPUP (0x80000000) harus
    dikonversi dulu supaya tidak ditolak ctypes sebagai overflow."""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _window_text(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_class(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def window_pid(hwnd) -> int:
    """PID proses pemilik jendela, atau 0."""
    if not IS_WINDOWS or not hwnd:
        return 0
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_alive(hwnd) -> bool:
    """False kalau jendela sudah dihancurkan (mis. context-nya ditutup)."""
    if not IS_WINDOWS or not hwnd:
        return False
    return bool(_user32.IsWindow(hwnd))


def find_window(title_fragment: str = None, timeout: float = 6.0, poll: float = 0.15,
                exclude=(), pid: int = 0):
    """HWND jendela Chromium top-level, atau None kalau tidak ketemu.

    Penyaringnya berlapis, dan itu memang perlu:
      - kelas   : hanya prefiks yang dicocokkan (lihat CHROMIUM_WINDOW_CLASS_PREFIX),
                  tapi prefiks ini dipakai juga oleh aplikasi Electron lain
      - judul   : `title_fragment` = penanda unik yang dipasang di document.title
      - pid     : kalau diisi, HANYA jendela milik proses itu yang diterima

    Tanpa `title_fragment` DAN tanpa `pid`, fungsi ini bisa mengembalikan jendela
    Spotify/Discord/VS Code yang kebetulan berkelas sama - jadi pemanggil wajib
    mengisi minimal salah satunya.

    Jendela yang sudah ditempelkan tidak lagi top-level, jadi tidak ikut
    terhitung di sini.

    Fungsi ini memakai time.sleep() - panggil lewat asyncio.to_thread() kalau
    dipakai dari dalam event loop.
    """
    if not IS_WINDOWS or (title_fragment is None and not pid):
        return None

    needle = title_fragment.lower() if title_fragment else None
    skip = set(exclude)
    deadline = time.monotonic() + timeout

    while True:
        found = []

        @_WNDENUMPROC
        def _callback(hwnd, _lparam):
            if hwnd in skip or not _user32.IsWindowVisible(hwnd):
                return True
            if not _window_class(hwnd).startswith(CHROMIUM_WINDOW_CLASS_PREFIX):
                return True
            if pid and window_pid(hwnd) != pid:
                return True
            title = _window_text(hwnd)
            if not title:
                return True
            if needle is None or needle in title.lower():
                found.append(hwnd)
                return False
            return True

        _user32.EnumWindows(_callback, 0)
        if found:
            return found[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def embed(hwnd, parent_hwnd, width: int, height: int) -> bool:
    """Jadikan `hwnd` anak dari `parent_hwnd` dan lepas dekorasi jendelanya."""
    if not IS_WINDOWS or not hwnd or not parent_hwnd:
        return False
    try:
        style = _user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        style &= ~_DECORATION_STYLES
        style |= WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN
        _user32.SetWindowLongW(hwnd, GWL_STYLE, _signed32(style))

        ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        ex_style &= ~_DECORATION_EX_STYLES
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, _signed32(ex_style))

        if not _user32.SetParent(hwnd, parent_hwnd):
            return False

        _user32.SetWindowPos(
            hwnd, None, 0, 0, max(width, 1), max(height, 1),
            SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOZORDER | SWP_NOACTIVATE,
        )
        return True
    except Exception:
        return False


def resize(hwnd, width: int, height: int) -> bool:
    if not IS_WINDOWS or not hwnd or not _user32.IsWindow(hwnd):
        return False
    try:
        return bool(_user32.MoveWindow(hwnd, 0, 0, max(width, 1), max(height, 1), True))
    except Exception:
        return False


def set_visible(hwnd, visible: bool) -> bool:
    """Tampilkan/sembunyikan satu jendela anak.

    Jaring pengaman saat jendela pengganti muncul sebelum jendela lama benar-
    benar hilang: yang lama disembunyikan supaya tidak menumpuk di panel.
    """
    if not IS_WINDOWS or not hwnd or not _user32.IsWindow(hwnd):
        return False
    try:
        _user32.ShowWindow(hwnd, SW_SHOWNA if visible else SW_HIDE)
        return True
    except Exception:
        return False


def release(hwnd) -> bool:
    """Kembalikan jendela jadi top-level lagi.

    Dipanggil sebelum jendela Tk dihancurkan: jendela anak akan ikut mati
    bersama parent-nya, dan itu membuat Chromium tutup mendadak di tengah
    pembersihan Playwright.
    """
    if not IS_WINDOWS or not hwnd or not _user32.IsWindow(hwnd):
        return False
    try:
        style = _user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        style &= ~WS_CHILD
        style |= WS_OVERLAPPEDWINDOW | WS_VISIBLE
        _user32.SetWindowLongW(hwnd, GWL_STYLE, _signed32(style))
        _user32.SetParent(hwnd, None)
        _user32.SetWindowPos(
            hwnd, None, 80, 80, 1100, 760,
            SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOZORDER | SWP_NOACTIVATE,
        )
        return True
    except Exception:
        return False
