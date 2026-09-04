"""GUI untuk menjalankan report TikTok dengan browser tertanam."""

import asyncio
import builtins
import io
import os
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

import config
import main as report_main
import win_embed
from tiktok_login import clear_session_file, session_belongs_to, session_owner

ENV_PATH = ".env"
# Jeda sebelum report berjalan otomatis setelah akun berganti. Memberi waktu
# log tampil dan memastikan worker sebelumnya benar-benar sudah berhenti.
AUTO_START_DELAY_MS = 1200
ACCOUNT_KEYS = config.ACCOUNT_KEYS
ACCOUNT_HEADER_PATTERN = config.ACCOUNT_HEADER_PATTERN
ACCOUNT_VALUE_PATTERN = config.ACCOUNT_VALUE_PATTERN
load_saved_accounts = config.load_saved_accounts

BG = "#12141a"
PANEL = "#1b1e26"
FIELD = "#252935"
BORDER = "#2f3441"
TEXT = "#e7e9ef"
MUTED = "#8a91a3"
ACCENT = "#fe2c55"
OK = "#3ddc97"
WARN = "#ffb454"
INFO = "#4cc2ff"

LOG_TAG_COLORS = {
    "ERROR": ACCENT,
    "WARNING": WARN,
    "SUCCESS": OK,
    "REPORT": TEXT,
    "LOGIN": INFO,
    "LOGOUT": INFO,
    "OTP": "#c98bff",
    "CAPTCHA": WARN,
    "TIME": MUTED,
    "INFO": MUTED,
    "GUI": MUTED,
    "SESI": MUTED,
    "POPUP": MUTED,
    "CONFIG": ACCENT,
    "INPUT": INFO,
    "MAIN": TEXT,
}

TAG_PATTERN = re.compile(r"^\s*\[([A-Z]+)\]")

# Batas tinggi yang diminta kolom kiri ke grid; lebih dari ini dialihkan ke
# scrollbar supaya panel log tidak pernah tergusur habis.
LEFT_MAX_HEIGHT = 560


class StdoutToQueue:
    """Alihkan print() ke Queue, sambil tetap meneruskan ke stdout asli.

    Dipotong per baris supaya panel log bisa mewarnai tiap baris sesuai tag
    [ERROR]/[LOGIN]/dst. Baris yang belum diakhiri newline ditahan di buffer.
    """

    def __init__(self, ui_queue, mirror=None):
        self._queue = ui_queue
        self._mirror = mirror
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return 0
        if self._mirror is not None:
            try:
                self._mirror.write(text)
            except Exception:
                pass
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._queue.put(("log", line))
        return len(text)

    def flush(self):
        if self._mirror is not None:
            try:
                self._mirror.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def reconfigure(self, **kwargs):
        pass

    @property
    def encoding(self):
        return "utf-8"


def write_env_file(updates: dict, path=ENV_PATH):
    """Perbarui nilai di .env tanpa membuang komentar/urutan yang sudah ada."""
    lines = []
    if os.path.exists(path):
        with io.open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    written = set()
    result = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            result.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            result.append(line)

    for key, value in updates.items():
        if key not in written:
            result.append(f"{key}={value}")

    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(result).rstrip("\n") + "\n")


class ReportApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ui_queue = queue.Queue()
        self.answer_queue = queue.Queue()

        self.worker = None
        self.loop = None
        self.task = None
        self.stopping = False
        self.waiting_input = False

        self.embedded = []
        self.embed_lock = threading.Lock()
        self.embed_enabled = win_embed.IS_WINDOWS
        self.embed_seq = 0
        self.browser_pid = 0
        self.host_hwnd = None
        self.host_size = (900, 700)
        self.saved_accounts = []
        self.saved_accounts_path = None
        self.active_account_index = None
        self._rotate_timer = None
        self._last_ran_account_index = None
        self.email_account_value = ""

        # Batch-run state (satu klik Jalankan untuk semua akun)
        self._batch_running = False
        self._batch_url = None
        self._batch_start_index = None
        self._batch_success = 0
        self._batch_fail = 0
        # Rincian per akun: (nomor, label, berhasil, catatan)
        self._batch_results = []

        self._build_ui()

        # Jangan gunakan kredensial .env secara diam-diam di jalur GUI.
        config.apply_credentials("", "", "", "")

        self._original_stdout = sys.stdout
        sys.stdout = StdoutToQueue(self.ui_queue, self._original_stdout)
        self._original_input = builtins.input

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain_ui_queue)

        # Tidak ada file akun default: pengguna harus memilih sendiri lewat
        # tombol "Pilih File". Memuat akun.txt diam-diam membuat kredensial
        # milik orang lain ikut terpakai kalau file itu tertinggal di folder.
        self._log_line("[GUI] Siap. Pilih file akun atau isi kredensial manual, lalu tekan Jalankan.")
        if not win_embed.IS_WINDOWS:
            self._log_line("[GUI] Bukan Windows: browser akan tampil sebagai jendela terpisah.")

    def _build_ui(self):
        root = self.root
        root.title("TikTok Auto Report")
        root.configure(bg=BG)
        # Ukuran awal harus sanggup memuat kolom kiri utuh + panel log, tapi
        # tidak boleh melebihi layar: di laptop 1366x768 jendela 800px membuat
        # baris jawaban OTP jatuh di bawah tepi layar. Kalau pengguna
        # memperkecilnya, kolom kiri beralih ke mode gulir.
        width = min(1280, max(1024, root.winfo_screenwidth() - 80))
        height = min(800, max(560, root.winfo_screenheight() - 100))
        root.geometry(f"{width}x{height}")
        root.minsize(1024, 560)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=TEXT, fieldbackground=FIELD)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 9))
        style.configure("Head.TLabel", background=BG, foreground=TEXT,
                        font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED,
                        font=("Segoe UI", 8))
        style.configure("Status.TLabel", background=BG, foreground=MUTED,
                        font=("Segoe UI", 9))
        style.configure("Prompt.TLabel", background=PANEL, foreground=WARN,
                        font=("Segoe UI Semibold", 9))
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        insertcolor=TEXT, padding=5)
        style.configure("TCheckbutton", background=PANEL, foreground=MUTED,
                        font=("Segoe UI", 8))
        style.map("TCheckbutton", background=[("active", PANEL)],
                  foreground=[("active", TEXT)])
        style.configure("Run.TButton", background=ACCENT, foreground="#ffffff",
                        font=("Segoe UI Semibold", 10), borderwidth=0, padding=(10, 8))
        style.map("Run.TButton", background=[("active", "#ff4d70"), ("disabled", "#5c2233")],
                  foreground=[("disabled", "#b9a0a7")])
        # Scrollbar clam bawaan berwarna terang; tanpa style ini ia menyala
        # putih di tengah panel gelap.
        style.configure("Dark.Vertical.TScrollbar", background=FIELD,
                        troughcolor=PANEL, bordercolor=PANEL, arrowcolor=MUTED,
                        darkcolor=FIELD, lightcolor=FIELD, borderwidth=0,
                        relief="flat", width=12)
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", BORDER), ("pressed", BORDER)],
                  arrowcolor=[("active", TEXT)])
        style.configure("Ghost.TButton", background=FIELD, foreground=TEXT,
                        font=("Segoe UI", 9), borderwidth=0, padding=(10, 8))
        style.map("Ghost.TButton", background=[("active", BORDER), ("disabled", "#1f222b")],
                  foreground=[("disabled", MUTED)])

        # uniform="main" membuat bobot dibaca sebagai proporsi lebar total,
        # bukan cuma pembagian sisa ruang: kolom form 25%, panel browser 75%.
        # minsize tetap jadi lantai supaya isi form tidak terjepit di jendela
        # sempit (1024px), di mana 25% kurang dari lebar kartu.
        root.columnconfigure(0, weight=1, uniform="main", minsize=320)
        root.columnconfigure(1, weight=3, uniform="main")
        root.rowconfigure(0, weight=2, minsize=260)
        root.rowconfigure(1, weight=3, minsize=240)

        left = self._build_left_column(root)

        self._build_target_card(left)
        self._build_credential_card(left)
        self._build_actions(left)
        self._bind_left_wheel()
        self._sync_left_scroll()

        self._build_browser_panel(root)
        self._build_log_card(root, row=1)

    def _build_left_column(self, root):
        """Bungkus kolom kiri dalam Canvas yang bisa digulir.

        Tanpa ini tinggi baris grid dibagi menurut weight, jadi isi kolom kiri
        terpotong begitu jendela lebih pendek dari kebutuhannya - tombol
        Jalankan/Stop/Hapus Sesi ikut hilang, terutama saat input manual dibuka.
        """
        host = ttk.Frame(root)
        host.grid(row=0, column=0, sticky="nsew")
        host.rowconfigure(0, weight=1)
        host.columnconfigure(0, weight=1)

        # width kecil disengaja: lebar minta bawaan Canvas (378px) jadi lantai
        # lebar kolom kiri dan menggagalkan pembagian 1/5 : 4/5 di grid root.
        self.left_canvas = tk.Canvas(host, bg=BG, highlightthickness=0, bd=0,
                                     takefocus=0, yscrollincrement=18, width=1)
        self.left_canvas.grid(row=0, column=0, sticky="nsew")
        self.left_scrollbar = ttk.Scrollbar(host, orient="vertical",
                                            style="Dark.Vertical.TScrollbar",
                                            command=self.left_canvas.yview)
        self.left_canvas.configure(yscrollcommand=self.left_scrollbar.set)

        # Padding bawah dibuat tipis: sisa ruang kosong di bawah tombol aksi
        # bikin scrollbar muncul padahal tidak ada isi yang tersembunyi.
        self.left_inner = ttk.Frame(self.left_canvas, padding=(14, 12, 7, 4))
        self.left_inner.columnconfigure(0, weight=1)
        self._left_window = self.left_canvas.create_window(
            (0, 0), window=self.left_inner, anchor="nw"
        )
        self.left_inner.bind("<Configure>", lambda _event: self._sync_left_scroll())
        self.left_canvas.bind("<Configure>", self._on_left_canvas_resize)
        return self.left_inner

    def _on_left_canvas_resize(self, event):
        # Isi selalu selebar panel; hanya tingginya yang digulir.
        self.left_canvas.itemconfigure(self._left_window, width=event.width)
        self._sync_left_scroll()

    def _sync_left_scroll(self):
        """Scrollbar kiri hanya muncul saat isi memang lebih tinggi dari panel."""
        needed = self.left_inner.winfo_reqheight()
        visible = self.left_canvas.winfo_height()
        if visible <= 1:
            # Belum dipetakan; tinggi sebenarnya baru diketahui saat <Configure>.
            return
        self.left_canvas.configure(
            scrollregion=(0, 0, self.left_canvas.winfo_width(), needed)
        )
        # Canvas tidak mewarisi tinggi isinya, jadi tanpa ini grid menganggap
        # kolom kiri hampir tidak butuh ruang dan panel log memakan semuanya.
        wanted = min(needed, LEFT_MAX_HEIGHT)
        if int(self.left_canvas.cget("height")) != wanted:
            self.left_canvas.configure(height=wanted)
        # Toleransi kecil: sisa beberapa piksel di bawah cuma padding kartu,
        # jangan sampai memicu scrollbar yang tidak perlu.
        if needed > visible + 10:
            if not self.left_scrollbar.winfo_ismapped():
                self.left_scrollbar.grid(row=0, column=1, sticky="ns")
        elif self.left_scrollbar.winfo_ismapped():
            self.left_scrollbar.grid_remove()
            self.left_canvas.yview_moveto(0)

    def _bind_left_wheel(self):
        """Roda mouse menggulir kolom kiri, tanpa mengganggu panel log."""
        def on_wheel(event):
            if not self.left_scrollbar.winfo_ismapped():
                return None
            if getattr(event, "num", 0) in (4, 5):
                step = -3 if event.num == 4 else 3
            else:
                step = -3 if event.delta > 0 else 3
            self.left_canvas.yview_scroll(step, "units")
            return "break"

        targets = [self.left_canvas, self.left_inner]
        while targets:
            widget = targets.pop()
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, on_wheel, add="+")
            targets.extend(widget.winfo_children())

    def _wrap_with_parent(self, label, slack=0):
        """Ikatkan wraplength ke lebar kontainer.

        Nilai wraplength tetap membuat teks panjang (prompt OTP, label akun)
        terpotong di kanan begitu jendela lebih sempit dari angka itu.
        """
        def resize(event):
            width = max(event.width - slack, 160)
            if int(label.cget("wraplength")) != width:
                label.configure(wraplength=width)

        label.master.bind("<Configure>", resize, add="+")

    def _card(self, parent, title, row, columnspan=1):
        wrapper = ttk.Frame(parent)
        wrapper.grid(row=row, column=0, columnspan=columnspan, sticky="nsew", pady=(0, 10))
        wrapper.columnconfigure(0, weight=1)
        ttk.Label(wrapper, text=title, style="Head.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        card = ttk.Frame(wrapper, style="Card.TFrame", padding=12)
        card.grid(row=1, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        wrapper.rowconfigure(1, weight=1)
        return card

    def _build_target_card(self, parent):
        card = self._card(parent, "1. Target Video", 0)

        ttk.Label(card, text="Kode / link video").grid(row=0, column=0, sticky="w")
        self.video_var = tk.StringVar()
        entry = ttk.Entry(card, textvariable=self.video_var)
        entry.grid(row=1, column=0, sticky="ew", pady=(3, 4))
        entry.focus_set()

        ttk.Label(
            card,
            style="Muted.TLabel",
            justify="left",
            text=("Terima salah satu: link penuh (.../@nama/video/123...),\n"
                  "short link vt.tiktok.com/XXXX, atau kode video saja."),
        ).grid(row=2, column=0, sticky="w")

    def _build_credential_card(self, parent):
        card = self._card(parent, "2. Kredensial", 1)

        self.email_var = tk.StringVar()
        self.tiktok_password_var = tk.StringVar()
        self.app_password_var = tk.StringVar()

        ttk.Label(card, text="File akun (opsional)").grid(
            row=0, column=0, sticky="w"
        )
        picker = ttk.Frame(card, style="Card.TFrame")
        picker.grid(row=1, column=0, sticky="ew", pady=(3, 3))
        picker.columnconfigure(0, weight=1)
        self.account_file_var = tk.StringVar(value="Belum ada file dipilih.")
        file_entry = ttk.Entry(picker, textvariable=self.account_file_var, state="readonly")
        file_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            picker, text="Pilih file", style="Ghost.TButton",
            command=self._choose_accounts_file,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Button(
            picker, text="Muat ulang", style="Ghost.TButton",
            command=self._reload_saved_accounts,
        ).grid(row=0, column=2, sticky="ew", padx=(8, 0))

        ttk.Label(card, text="Akun aktif").grid(row=2, column=0, sticky="w")
        self.active_account_var = tk.StringVar(value="Belum ada akun dimuat.")
        active_label = ttk.Label(
            card, textvariable=self.active_account_var, style="Muted.TLabel",
            wraplength=380, justify="left",
        )
        active_label.grid(row=3, column=0, sticky="w", pady=(3, 3))
        self._wrap_with_parent(active_label, slack=28)
        self.next_account_button = ttk.Button(
            card, text="Akun berikutnya", style="Ghost.TButton",
            command=self._next_account_and_run, state="disabled",
        )
        self.next_account_button.grid(row=4, column=0, sticky="ew", pady=(2, 3))

        self.auto_rotate_var = tk.BooleanVar(value=True)
        self.auto_rotate_check = ttk.Checkbutton(
            card, text="Rotasi otomatis ke akun berikutnya",
            variable=self.auto_rotate_var, style="TCheckbutton",
            command=self._on_auto_rotate_toggle,
        )
        self.auto_rotate_check.grid(row=5, column=0, sticky="w", pady=(2, 3))

        self.account_source_var = tk.StringVar(
            value="Pilih file dulu; kredensial tidak dibaca otomatis saat aplikasi dibuka."
        )
        source_label = ttk.Label(
            card, textvariable=self.account_source_var, style="Muted.TLabel",
            wraplength=380, justify="left",
        )
        source_label.grid(row=6, column=0, sticky="w", pady=(0, 5))
        self._wrap_with_parent(source_label, slack=28)

        self.manual_fields_visible = False
        self.manual_fields_button = ttk.Button(
            card, text="Tampilkan input manual", style="Ghost.TButton",
            command=self._toggle_manual_fields,
        )
        self.manual_fields_button.grid(row=7, column=0, sticky="ew", pady=(2, 0))

        self.manual_fields = ttk.Frame(card, style="Card.TFrame")
        self.manual_fields.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        self.manual_fields.columnconfigure(0, weight=1)
        self._labeled_entry(self.manual_fields, 0, "Email akun TikTok", self.email_var)
        self.tiktok_password_entry = self._labeled_entry(
            self.manual_fields, 2, "Password TikTok", self.tiktok_password_var, secret=True
        )
        self.app_password_entry = self._labeled_entry(
            self.manual_fields, 4, "App Password Gmail (untuk baca OTP)", self.app_password_var, secret=True
        )

        options = ttk.Frame(self.manual_fields, style="Card.TFrame")
        options.grid(row=6, column=0, sticky="ew", pady=(8, 0))

        self.show_secret_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options, text="Tampilkan password", variable=self.show_secret_var,
            command=self._toggle_secret, style="TCheckbutton",
        ).grid(row=0, column=0, sticky="w")

        self.save_env_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options, text="Simpan ke .env (untuk CLI)", variable=self.save_env_var,
            style="TCheckbutton",
        ).grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.manual_fields.grid_remove()

    def _choose_accounts_file(self):
        """Minta pengguna memilih file sebelum kredensial dibaca."""
        initial_dir = (
            os.path.dirname(self.saved_accounts_path)
            if self.saved_accounts_path else os.getcwd()
        )
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Pilih file akun",
            initialdir=initial_dir,
            filetypes=[("File teks", "*.txt"), ("Semua file", "*.*")],
        )
        if path:
            self._load_accounts_from_file(path)

    def _reload_saved_accounts(self):
        """Segarkan daftar dari file yang sebelumnya dipilih pengguna."""
        if not self.saved_accounts_path:
            self.account_source_var.set("Pilih file akun terlebih dahulu.")
            return
        self._load_accounts_from_file(self.saved_accounts_path)

    def _load_accounts_from_file(self, path):
        """Muat file lalu siapkan akun pertama, tanpa menjalankan report."""
        source, accounts, error = load_saved_accounts(path)
        self.saved_accounts = accounts
        self.active_account_index = None
        self.active_account_var.set("Belum ada akun dimuat.")
        self.next_account_button.configure(state="disabled")
        self.account_file_var.set(source or "Belum ada file dipilih.")
        if source:
            self.saved_accounts_path = source

        if error:
            self.account_source_var.set("Gagal membaca file akun.")
            self._log_line(f"[WARNING] Gagal membaca file akun: {error}")
        elif accounts:
            message = f"{len(accounts)} akun tersedia. Akun pertama telah dimuat ke formulir."
            self.account_source_var.set(message)
            self._log_line(f"[GUI] {len(accounts)} akun tersedia dari file yang dipilih.")
            self._set_active_account(0)
        else:
            self.account_source_var.set("Tidak ada blok akun yang terbaca dari file ini.")

    def _set_active_account(self, index):
        """Isi formulir dari satu akun yang dipilih oleh alur manual GUI."""
        if index < 0 or index >= len(self.saved_accounts):
            return

        self.active_account_index = index
        account = self.saved_accounts[index]
        values = account["values"]
        self.email_var.set(values.get("TIKTOK_EMAIL", ""))
        self.tiktok_password_var.set(values.get("TIKTOK_PASSWORD", ""))
        self.app_password_var.set(values.get("EMAIL_APP_PASSWORD", ""))
        # Memilih profil tidak boleh membuat kredensial tak sengaja ditulis ke
        # .env. Pengguna tetap dapat mencentang opsi itu dengan sadar.
        self.save_env_var.set(False)
        self.active_account_var.set(
            f"{index + 1} dari {len(self.saved_accounts)} — {account['label']}"
        )
        self._refresh_next_account_button()
        self._log_line(f"[GUI] Kredensial dimuat untuk akun {index + 1}.")

    def _on_auto_rotate_toggle(self):
        if not self.auto_rotate_var.get() and self._rotate_timer is not None:
            self._cancel_auto_start()
            self._log_line("[GUI] Rotasi otomatis dimatikan, auto-run dibatalkan.")
            self._halt_auto(ok=False)

    def _refresh_next_account_button(self):
        index = self.active_account_index
        has_next = index is not None and index + 1 < len(self.saved_accounts)
        running = bool(self.worker and self.worker.is_alive())
        self.next_account_button.configure(
            state="normal" if has_next and not running else "disabled"
        )

    def _toggle_manual_fields(self):
        self.manual_fields_visible = not self.manual_fields_visible
        if self.manual_fields_visible:
            self.manual_fields.grid()
            self.manual_fields_button.configure(text="Sembunyikan input manual")
        else:
            self.manual_fields.grid_remove()
            self.manual_fields_button.configure(text="Tampilkan input manual")
        self.root.update_idletasks()
        self._sync_left_scroll()

    def _next_account(self):
        """Pindah ke akun berikutnya tanpa konfirmasi - hapus sesi lama langsung.

        Return True kalau akun benar-benar berpindah.
        """
        if self.worker and self.worker.is_alive():
            return False
        if self.active_account_index is None:
            return False

        next_index = self.active_account_index + 1
        if next_index >= len(self.saved_accounts):
            self._log_line("[GUI] Sudah di akun terakhir, tidak ada akun berikutnya.")
            return False

        if os.path.exists(config.STORAGE_STATE_PATH):
            clear_session_file("berpindah ke akun berikutnya")
        self._set_active_account(next_index)
        self._log_line(f"[GUI] Beralih ke akun {next_index + 1} dari {len(self.saved_accounts)}.")
        return True

    def _next_account_and_run(self):
        """Tombol 'Akun Berikutnya': ganti akun lalu jalankan report otomatis."""
        if not self._next_account():
            return
        self._queue_auto_start(f"akun {self.active_account_index + 1}")

    def _cancel_auto_start(self):
        """Batalkan auto-start yang masih menunggu di timer Tk."""
        timer, self._rotate_timer = self._rotate_timer, None
        if timer is not None:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass

    def _queue_auto_start(self, reason: str):
        """Antrikan "klik" Jalankan Report otomatis setelah akun berganti."""
        self._cancel_auto_start()
        if self.worker and self.worker.is_alive():
            return
        if not self.auto_rotate_var.get():
            self._log_line("[GUI] Rotasi otomatis mati - tekan Jalankan Report bila mau lanjut.")
            self._halt_auto(ok=False)
            return
        if not self.video_var.get().strip():
            self._log_line("[WARNING] Kode/link video belum diisi, report tidak dijalankan otomatis.")
            self._halt_auto(ok=False)
            return
        self._log_line(f"[GUI] Report berjalan otomatis untuk {reason}...")
        self._set_status(f"Menyiapkan report otomatis ({reason})...")
        self._rotate_timer = self.root.after(AUTO_START_DELAY_MS, self._auto_start_now)

    def _auto_start_now(self):
        self._rotate_timer = None
        if self.worker and self.worker.is_alive():
            return
        self._start(auto=True)

    def _labeled_entry(self, card, row, label, variable, secret=False):
        ttk.Label(card, text=label).grid(row=row, column=0, sticky="w", pady=(4, 0))
        entry = ttk.Entry(card, textvariable=variable, show="•" if secret else "")
        entry.grid(row=row + 1, column=0, sticky="ew", pady=(3, 0))
        return entry

    def _build_actions(self, parent):
        bar = ttk.Frame(parent)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, 2))
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=0)
        bar.columnconfigure(2, weight=0)

        self.run_button = ttk.Button(bar, text="Jalankan Report", style="Run.TButton",
                                     command=self._start)
        self.run_button.grid(row=0, column=0, sticky="ew")

        self.stop_button = ttk.Button(bar, text="Stop", style="Ghost.TButton",
                                      command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self.reset_button = ttk.Button(bar, text="Hapus Sesi", style="Ghost.TButton",
                                       command=self._clear_session)
        self.reset_button.grid(row=0, column=2, sticky="ew", padx=(8, 0))

    def _build_log_card(self, parent, row=3, columnspan=1):
        # Kartu log menempel langsung ke root, jadi marjinnya dipasang sendiri.
        # Marjin kanan disamakan dengan padding kolom kiri (7) supaya lebarnya
        # persis sama dengan kartu Target Video dan Kredensial di atasnya.
        holder = ttk.Frame(parent, padding=(14, 0, 7, 12))
        holder.grid(row=row, column=0, columnspan=columnspan, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

        card = self._card(holder, "3. Log", 0)
        card.rowconfigure(0, weight=1)

        log_frame = tk.Frame(card, bg=BORDER, highlightthickness=0, bd=0)
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log = tk.Text(
            log_frame, bg="#0e1015", fg=TEXT, insertbackground=TEXT,
            font=("Consolas", 11), wrap="word", relief="flat", padx=8, pady=6,
            state="disabled", height=7, width=1,
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical",
                                  style="Dark.Vertical.TScrollbar",
                                  command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        for name, color in LOG_TAG_COLORS.items():
            self.log.tag_configure(name, foreground=color)
        self.log.tag_configure("PLAIN", foreground=TEXT)
        self.log.tag_configure("PROMPT", foreground=WARN, font=("Consolas", 11, "bold"))

        prompt_box = ttk.Frame(card, style="Card.TFrame")
        prompt_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        prompt_box.columnconfigure(0, weight=1)

        self.prompt_var = tk.StringVar(value="Tidak ada langkah manual yang menunggu.")
        self.prompt_label = ttk.Label(prompt_box, textvariable=self.prompt_var,
                                      style="Muted.TLabel", wraplength=600,
                                      justify="left")
        self.prompt_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self._wrap_with_parent(self.prompt_label, slack=8)

        self.answer_var = tk.StringVar()
        self.answer_entry = ttk.Entry(prompt_box, textvariable=self.answer_var,
                                      state="disabled")
        self.answer_entry.grid(row=1, column=0, sticky="ew")
        self.answer_entry.bind("<Return>", lambda _event: self._send_answer())

        self.answer_button = ttk.Button(prompt_box, text="Kirim", style="Ghost.TButton",
                                        command=self._send_answer, state="disabled")
        self.answer_button.grid(row=1, column=1, sticky="ew", padx=(8, 0))

        footer = ttk.Frame(card, style="Card.TFrame")
        footer.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(footer, text="Bersihkan log", style="Ghost.TButton",
                   command=self._clear_log).grid(row=0, column=0, sticky="w")

    def _build_browser_panel(self, root):
        right = ttk.Frame(root, padding=(7, 12, 14, 12))
        # rowspan=2: panel browser memakai seluruh tinggi jendela di kolom
        # kanan, sementara kolom kiri dibagi antara form dan log.
        right.grid(row=0, column=1, rowspan=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        header = ttk.Frame(right)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="4. Browser", style="Head.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel",
                  anchor="e").grid(row=0, column=1, sticky="e")

        self.browser_host = tk.Frame(right, bg="#0b0d11", highlightthickness=1,
                                     highlightbackground=BORDER, bd=0)
        self.browser_host.grid(row=1, column=0, sticky="nsew")
        self.browser_host.grid_propagate(False)
        self.browser_host.bind("<Configure>", self._on_host_resize)

        self.placeholder = tk.Label(
            self.browser_host,
            text="Jendela browser akan muncul di sini\nsetelah report dijalankan.",
            bg="#0b0d11", fg=MUTED, font=("Segoe UI", 10), justify="center",
        )
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

    def _toggle_secret(self):
        show = "" if self.show_secret_var.get() else "•"
        self.tiktok_password_entry.configure(show=show)
        self.app_password_entry.configure(show=show)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _log_line(self, line: str):
        match = TAG_PATTERN.match(line)
        tag = match.group(1) if match and match.group(1) in LOG_TAG_COLORS else "PLAIN"
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def _set_status(self, text: str):
        self.status_var.set(text)

    def _on_host_resize(self, event):
        self.host_size = (max(event.width - 2, 100), max(event.height - 2, 100))
        with self.embed_lock:
            current = self.embedded[-1] if self.embedded else None
        if current:
            win_embed.resize(current, *self.host_size)

    def _gui_input(self, prompt: str = "") -> str:
        """Ganti input() bawaan: bertanya di panel log, bukan di terminal.

        Dipanggil dari thread worker dan memblokirnya sampai tombol Kirim
        ditekan - sama seperti input() memblokir di versi CLI.
        """
        text = str(prompt).strip() or "Langkah manual diperlukan."
        print(f"[GUI] Menunggu masukan: {text}")
        self.ui_queue.put(("prompt", text))
        try:
            answer = self.answer_queue.get()
        except Exception:
            answer = ""
        self.ui_queue.put(("prompt_done", ""))
        print(f"[GUI] Masukan diterima ({len(answer)} karakter).")
        return answer

    def _send_answer(self):
        if not self.waiting_input:
            return
        answer = self.answer_var.get()
        self.answer_var.set("")
        self._set_prompt_idle()
        self.answer_queue.put(answer)

    def _set_prompt_idle(self):
        self.waiting_input = False
        self.prompt_var.set("Tidak ada langkah manual yang menunggu.")
        self.prompt_label.configure(style="Muted.TLabel")
        self.answer_entry.configure(state="disabled")
        self.answer_button.configure(state="disabled")

    def _set_prompt_active(self, text: str):
        self.waiting_input = True
        self.prompt_var.set(f"Perlu masukan: {text}  (kosongkan lalu Kirim = Enter)")
        self.prompt_label.configure(style="Prompt.TLabel")
        self.answer_entry.configure(state="normal")
        self.answer_button.configure(state="normal")
        self.answer_entry.focus_set()

    def _collect_inputs(self):
        """Validasi form. Return (url, error_message)."""
        raw_video = self.video_var.get()
        email = self.email_var.get().strip()
        tiktok_password = self.tiktok_password_var.get()
        app_password = self.app_password_var.get().strip()
        inbox = email

        if not raw_video.strip():
            return None, "Kode/link video belum diisi."
        url = report_main.normalize_tiktok_url(raw_video)
        if not url:
            return None, ("Kode/link video tidak dikenali.\n\n"
                          "Pakai link penuh yang memuat /video/, short link "
                          "vt.tiktok.com, atau kode video (angka) saja.")
        if not email:
            return None, "Email akun TikTok belum diisi."
        if not tiktok_password:
            return None, "Password TikTok belum diisi."
        if not app_password:
            return None, ("App Password Gmail belum diisi. Ini bukan password "
                          "akun biasa - buat di myaccount.google.com/apppasswords.")

        # Sesi milik akun lain harus dibuang SEBELUM browser dijalankan.
        # Pemeriksaannya berbasis email, bukan cuma "apakah tadi rotasi akun",
        # supaya input manual dan restart aplikasi ikut terlindungi.
        if os.path.exists(config.STORAGE_STATE_PATH) and not session_belongs_to(email):
            owner = session_owner()
            milik = owner if owner else "akun yang tidak tercatat"
            self._log_line(f"[SESI] Sesi tersimpan milik {milik}, bukan {email}.")
            clear_session_file("sesi milik akun lain")

        config.apply_credentials(
            tiktok_email=email,
            tiktok_password=tiktok_password,
            email_account=inbox,
            email_app_password=app_password,
        )

        if self.save_env_var.get():
            try:
                write_env_file({
                    "TIKTOK_EMAIL": email,
                    "TIKTOK_PASSWORD": tiktok_password,
                    "EMAIL_ACCOUNT": inbox,
                    "EMAIL_APP_PASSWORD": app_password,
                })
                self._log_line(f"[GUI] Kredensial disimpan ke {ENV_PATH}.")
            except Exception as e:
                self._log_line(f"[WARNING] Gagal menyimpan {ENV_PATH}: {e}")

        return url, None

    def _start(self, auto: bool = False):
        if self.worker and self.worker.is_alive():
            return
        if not auto:
            # Klik manual membatalkan auto-start yang masih menunggu.
            self._cancel_auto_start()

        url, error = self._collect_inputs()
        if error:
            if not auto:
                messagebox.showwarning("Belum bisa dijalankan", error, parent=self.root)
                return
            # Jalur otomatis tidak boleh membuka dialog modal: dialog itu
            # menghentikan seluruh batch sampai ada orang yang menutupnya.
            self._log_line(f"[ERROR] Akun ini dilewati: {error.splitlines()[0]}")
            self._skip_current_account()
            return

        # Inisialisasi sesi batch baru setiap kali user menekan tombol
        if not self._batch_running:
            self._batch_running = True
            self._batch_url = url
            self._batch_start_index = self.active_account_index
            self._batch_success = 0
            self._batch_fail = 0
            self._batch_results = []
            total = len(self.saved_accounts)
            cur = (self.active_account_index or 0) + 1
            remaining = total - (self.active_account_index or 0)
            self._log_line(
                f"[GUI] Batch dimulai: {remaining} akun akan dijalankan "
                f"(akun {cur}–{total})."
            )

        self._start_run(self._batch_url or url)

    def _account_label(self):
        """Nama akun untuk baris ringkasan; jatuh ke email/manual bila perlu."""
        index = self.active_account_index
        if index is not None and 0 <= index < len(self.saved_accounts):
            return self.saved_accounts[index].get("label") or f"akun {index + 1}"
        return self.email_var.get().strip() or "input manual"

    def _record_result(self, ok: bool, note: str = ""):
        """Catat hasil satu akun supaya ringkasan akhir bisa dirinci."""
        if ok:
            self._batch_success += 1
        else:
            self._batch_fail += 1
        number = (self.active_account_index or 0) + 1
        self._batch_results.append((number, self._account_label(), ok, note))

    def _skip_current_account(self):
        """Data akun tidak lengkap di jalur otomatis: hitung gagal lalu lanjut."""
        if self._batch_running:
            self._record_result(False, "data akun tidak lengkap")
        if self._can_rotate():
            self._next_account()
            self._queue_auto_start(f"akun {self.active_account_index + 1}")
            return
        self._end_batch(ok=False)

    def _halt_auto(self, ok: bool):
        """Auto-run tidak bisa lanjut: tutup batch kalau ada, kalau tidak
        cukup pulihkan tombol supaya user bisa lanjut manual."""
        if self._batch_running:
            self._end_batch(ok=ok)
        else:
            self.run_button.configure(state="normal")
            self.reset_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self._refresh_next_account_button()

    def _can_rotate(self) -> bool:
        """True kalau rotasi otomatis aktif dan masih ada akun berikutnya."""
        return bool(
            self.auto_rotate_var.get()
            and self.active_account_index is not None
            and (self.active_account_index + 1) < len(self.saved_accounts)
        )

    def _end_batch(self, ok: bool, stopped: bool = False):
        """Tutup batch: cetak ringkasan bila perlu, lalu aktifkan tombol lagi."""
        self._cancel_auto_start()
        was_batch = self._batch_running
        self._batch_running = False
        if was_batch and self._batch_results:
            self._print_batch_summary(stopped=stopped)
        if stopped:
            self._set_status("Dihentikan")
        else:
            self._set_status("Laporan terkirim" if ok else "Selesai - belum terkonfirmasi")
        self.run_button.configure(state="normal")
        self.reset_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._refresh_next_account_button()

    def _start_run(self, url: str):
        """Jalankan satu siklus report (dipakai oleh _start dan auto-continue)."""
        self._cancel_auto_start()
        self.stopping = False
        self.embed_seq = 0
        self.browser_pid = 0
        self.embed_enabled = win_embed.IS_WINDOWS
        with self.embed_lock:
            self.embedded.clear()

        self.browser_host.update_idletasks()
        self.host_hwnd = self.browser_host.winfo_id()
        self.host_size = (
            max(self.browser_host.winfo_width() - 2, 100),
            max(self.browser_host.winfo_height() - 2, 100),
        )

        self.run_button.configure(state="disabled")
        self.reset_button.configure(state="disabled")
        self.next_account_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        cur = (self.active_account_index or 0) + 1
        total = len(self.saved_accounts) or 1
        self._set_status(f"Menjalankan... (akun {cur}/{total})")
        self._log_line("=" * 58)
        self._log_line(f"[GUI] Akun {cur}/{total} — target: {url}")

        builtins.input = self._gui_input
        # headless dipaksa mati: jendela yang tidak digambar tidak bisa
        # ditempelkan ke panel, dan captcha jadi tidak mungkin diselesaikan.
        self.worker = threading.Thread(target=self._worker_main, args=(url,), daemon=True)
        self.worker.start()

    def _worker_main(self, url: str):
        ok = False
        try:
            ok = asyncio.run(self._run_async(url))
        except asyncio.CancelledError:
            print("[GUI] Proses dihentikan.")
        except Exception as e:
            print(f"[ERROR] {type(e).__name__}: {e}")
            for line in traceback.format_exc().splitlines()[-4:]:
                print(f"[ERROR] {line}")
        finally:
            self.loop = None
            self.task = None
            self.ui_queue.put(("done", bool(ok)))

    async def _run_async(self, url: str) -> bool:
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        return await report_main.run_report(url, headless=False, on_page=self._attach_page)

    async def _attach_page(self, page):
        """Tempelkan jendela Chromium milik `page` ke panel kanan.

        Jendela dikenali lewat judul penanda yang dipasang sebentar sebelum
        halaman aslinya dibuka, lalu dikunci ke PID proses browser. Dua lapis
        ini wajib: kelas jendela Chromium juga dipakai semua aplikasi Electron
        (Spotify, Discord, VS Code), jadi pencarian tanpa penyaring bisa
        menyambar jendela aplikasi lain.
        """
        if not self.embed_enabled or not self.host_hwnd:
            return

        # Context yang ditutup membawa jendelanya sekalian.
        with self.embed_lock:
            self.embedded = [h for h in self.embedded if win_embed.is_alive(h)]
            known = list(self.embedded)

        self.embed_seq += 1
        marker = f"AR-EMBED-{os.getpid()}-{self.embed_seq}"
        try:
            await page.goto(f"data:text/html,<title>{marker}</title>",
                            wait_until="load", timeout=8000)
        except Exception:
            pass

        hwnd = await asyncio.to_thread(
            win_embed.find_window, marker, 8.0, 0.15, known, self.browser_pid
        )
        if not hwnd and self.browser_pid:
            # Penanda judul tidak terbaca (halaman sudah pindah lebih dulu).
            # Aman diambil tanpa judul HANYA karena PID browser sudah diketahui
            # dari jendela sebelumnya.
            hwnd = await asyncio.to_thread(
                win_embed.find_window, None, 3.0, 0.15, known, self.browser_pid
            )
        if not hwnd:
            print("[GUI] Jendela browser tidak ditemukan - dibiarkan terpisah.")
            self.embed_enabled = False
            return

        if not self.browser_pid:
            self.browser_pid = win_embed.window_pid(hwnd)

        width, height = self.host_size
        for old in known:
            win_embed.set_visible(old, False)

        if win_embed.embed(hwnd, self.host_hwnd, width, height):
            with self.embed_lock:
                self.embedded.append(hwnd)
            self.ui_queue.put(("embedded", ""))
            print("[GUI] Jendela browser ditempelkan ke panel kanan.")
        else:
            for old in known:
                win_embed.set_visible(old, True)
            print("[GUI] Gagal menempelkan jendela - dibiarkan terpisah.")
            self.embed_enabled = False

    def _stop(self):
        # Stop harus ikut mematikan auto-start yang menunggu di timer, kalau
        # tidak batch hidup lagi sesaat setelah user menekan Stop.
        pending = self._rotate_timer is not None
        self._cancel_auto_start()
        if not (self.worker and self.worker.is_alive()):
            if pending:
                self._log_line("[GUI] Auto-run dibatalkan.")
                self._end_batch(ok=False, stopped=True)
            return
        self.stopping = True
        self._set_status("Menghentikan...")
        self._log_line("[GUI] Permintaan stop dikirim.")

        loop, task = self.loop, self.task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
        # Kalau worker sedang menunggu jawaban manual, ia terblokir di
        # answer_queue.get() dan tidak akan pernah sampai ke titik pembatalan.
        if self.waiting_input:
            self.answer_queue.put("")
            self._set_prompt_idle()

    def _finish(self, ok: bool):
        # Event "done" dikirim worker dari blok finally, jadi thread-nya bisa
        # masih is_alive() sesaat di sini. Lepaskan referensinya lebih dulu
        # supaya guard is_alive() di jalur rotasi tidak menggantung batch.
        worker, self.worker = self.worker, None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)

        builtins.input = self._original_input
        self._set_prompt_idle()
        self._release_embedded()
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")

        if self.stopping:
            # User menekan Stop - akhiri batch dan tampilkan ringkasan parsial
            self._end_batch(ok=False, stopped=True)
            return

        # Catat hasil run ini
        if self._batch_running:
            self._record_result(ok)

        if self._batch_running and self._can_rotate():
            self._next_account()
            self._queue_auto_start(f"akun {self.active_account_index + 1}")
            return

        # Semua akun selesai atau rotasi otomatis dimatikan
        self._end_batch(ok=ok)

    def _print_batch_summary(self, stopped: bool = False):
        """Cetak ringkasan hasil batch ke panel log setelah semua akun selesai."""
        total = self._batch_success + self._batch_fail
        pending = max(len(self.saved_accounts) - total, 0) if self.saved_accounts else 0

        self._log_line("=" * 58)
        self._log_line("[GUI] ===== RINGKASAN BATCH =====")
        if stopped:
            self._log_line("[WARNING] Batch dihentikan sebelum semua akun selesai.")

        # Rincian per akun lebih dulu, supaya angka totalnya bisa ditelusuri.
        for number, label, ok, note in self._batch_results:
            if ok:
                self._log_line(f"[SUCCESS] Akun {number} - {label}: BERHASIL")
            else:
                alasan = f" ({note})" if note else ""
                self._log_line(f"[ERROR] Akun {number} - {label}: GAGAL{alasan}")

        self._log_line("-" * 58)
        # Tag log ikut tampil di panel, jadi padding dihitung bersama tag-nya
        # supaya titik dua tetap sejajar walau warna tiap baris berbeda.
        rows = [
            ("[GUI]", "Total akun dijalankan", total),
            ("[SUCCESS]", "Berhasil", self._batch_success),
            ("[ERROR]", "Gagal/tidak konfirmasi", self._batch_fail),
        ]
        if pending:
            rows.append(("[WARNING]", "Belum dijalankan", pending))
        for prefix, label, value in rows:
            self._log_line(f"{prefix} {label}".ljust(34) + f": {value}")
        self._log_line("=" * 58)

    def _release_embedded(self):
        with self.embed_lock:
            hwnds, self.embedded = list(self.embedded), []
        for hwnd in hwnds:
            win_embed.release(hwnd)

    def _clear_session(self):
        if self.worker and self.worker.is_alive():
            return
        if not os.path.exists(config.STORAGE_STATE_PATH):
            self._log_line("[SESI] Tidak ada file sesi tersimpan.")
            return
        clear_session_file("dihapus dari GUI")

    def _drain_ui_queue(self):
        # Dibatasi per siklus supaya banjir log tidak membekukan jendela.
        for _ in range(300):
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._log_line(payload)
            elif kind == "prompt":
                self._set_prompt_active(payload)
            elif kind == "prompt_done":
                self._set_prompt_idle()
            elif kind == "status":
                self._set_status(payload)
            elif kind == "embedded":
                self.placeholder.place_forget()
            elif kind == "done":
                self._finish(bool(payload))

        self.root.after(80, self._drain_ui_queue)

    def _on_close(self):
        self._cancel_auto_start()
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Masih berjalan",
                "Report masih berjalan. Hentikan dan tutup aplikasi?",
                parent=self.root,
            ):
                return
            self._stop()
            # Jendela anak ikut mati bersama parent-nya; dilepas dulu supaya
            # Chromium tidak tutup mendadak di tengah pembersihan Playwright.
            self._release_embedded()
            self.worker.join(timeout=8)

        self._release_embedded()
        builtins.input = self._original_input
        sys.stdout = self._original_stdout
        self.root.destroy()


def run():
    root = tk.Tk()
    ReportApp(root)
    root.mainloop()


if __name__ == "__main__":
    run()
