"""GUI untuk auto-report TikTok.

Tata letak:
    kiri atas   - form kode/link video
    kiri tengah - kredensial (email, password TikTok, app password) + tombol
    kiri bawah  - log berjalan + kolom jawaban untuk langkah manual
    kanan       - jendela Chromium yang dijalankan, ditempelkan ke dalam panel

Jalankan: python gui.py

Alur report-nya sendiri tetap milik main.run_report(); file ini hanya lapisan
tampilan. Tiga jembatan yang menghubungkannya:
  1. kredensial  -> config.apply_credentials()
  2. log         -> sys.stdout dialihkan ke Queue lalu digambar Tk
  3. input()     -> ditambal supaya bertanya di panel log, bukan di terminal
"""

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
from tkinter import messagebox, ttk

import config
import main as report_main
import win_embed
from tiktok_login import clear_session_file

ENV_PATH = ".env"

# Palet gelap. Merah TikTok dipakai hanya untuk aksi utama dan penanda error
# supaya tetap gampang dibedakan dari teks biasa.
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
        # main.py dan report.py memanggil sys.stdout.reconfigure() di Windows.
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

        # Jendela Chromium yang sudah ditempelkan. main.run_report() menjaga
        # hanya satu context hidup pada satu waktu, tapi daftar ini tetap
        # menyimpan riwayatnya: jendela lama disembunyikan, bukan diandaikan
        # sudah mati, supaya tidak ada sisa jendela yang menumpuk di panel.
        self.embedded = []
        self.embed_lock = threading.Lock()
        self.embed_enabled = win_embed.IS_WINDOWS
        self.embed_seq = 0
        # PID proses browser, dipelajari dari jendela pertama yang ketemu lewat
        # penanda judul. Selama masih 0, pencarian tanpa judul tidak dilakukan.
        self.browser_pid = 0
        self.host_hwnd = None
        self.host_size = (900, 700)

        self._build_ui()

        # Form kredensial SELALU dimulai kosong - tidak ada nilai bawaan dari
        # .env. Nilai yang sudah termuat di config saat import ikut dikosongkan
        # supaya tidak ada kredensial lama yang terpakai diam-diam kalau suatu
        # saat ada jalur kode yang lupa memanggil apply_credentials().
        config.apply_credentials("", "", "", "")

        self._original_stdout = sys.stdout
        sys.stdout = StdoutToQueue(self.ui_queue, self._original_stdout)
        self._original_input = builtins.input

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain_ui_queue)

        self._log_line("[GUI] Siap. Isi kode video dan kredensial, lalu tekan Jalankan.")
        if not win_embed.IS_WINDOWS:
            self._log_line("[GUI] Bukan Windows: browser akan tampil sebagai jendela terpisah.")

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = self.root
        root.title("TikTok Auto Report")
        root.configure(bg=BG)
        root.geometry("1420x860")
        root.minsize(1120, 720)

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
        style.configure("Ghost.TButton", background=FIELD, foreground=TEXT,
                        font=("Segoe UI", 9), borderwidth=0, padding=(10, 8))
        style.map("Ghost.TButton", background=[("active", BORDER), ("disabled", "#1f222b")],
                  foreground=[("disabled", MUTED)])

        root.columnconfigure(0, weight=0, minsize=460)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        left = ttk.Frame(root, padding=(14, 12, 7, 12))
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)

        self._build_target_card(left)
        self._build_credential_card(left)
        self._build_actions(left)
        self._build_log_card(left)

        self._build_browser_panel(root)

    def _card(self, parent, title, row):
        wrapper = ttk.Frame(parent)
        wrapper.grid(row=row, column=0, sticky="nsew", pady=(0, 10))
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
        self.inbox_var = tk.StringVar()

        self._labeled_entry(card, 0, "Email akun TikTok", self.email_var)
        self.tiktok_password_entry = self._labeled_entry(
            card, 2, "Password TikTok", self.tiktok_password_var, secret=True
        )
        self.app_password_entry = self._labeled_entry(
            card, 4, "App Password Gmail (untuk baca OTP)", self.app_password_var, secret=True
        )
        self._labeled_entry(
            card, 6, "Email inbox OTP (kosongkan kalau sama)", self.inbox_var
        )

        options = ttk.Frame(card, style="Card.TFrame")
        options.grid(row=8, column=0, sticky="ew", pady=(8, 0))

        self.show_secret_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options, text="Tampilkan password", variable=self.show_secret_var,
            command=self._toggle_secret, style="TCheckbutton",
        ).grid(row=0, column=0, sticky="w")

        # Menulis .env TIDAK membuat form ini terisi otomatis di run berikutnya
        # - .env hanya dibaca oleh jalur CLI (python main.py).
        self.save_env_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options, text="Simpan ke .env (untuk CLI)", variable=self.save_env_var,
            style="TCheckbutton",
        ).grid(row=0, column=1, sticky="w", padx=(14, 0))

    def _labeled_entry(self, card, row, label, variable, secret=False):
        ttk.Label(card, text=label).grid(row=row, column=0, sticky="w", pady=(4, 0))
        entry = ttk.Entry(card, textvariable=variable, show="•" if secret else "")
        entry.grid(row=row + 1, column=0, sticky="ew", pady=(3, 0))
        return entry

    def _build_actions(self, parent):
        bar = ttk.Frame(parent)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
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

    def _build_log_card(self, parent):
        card = self._card(parent, "3. Log", 3)
        card.rowconfigure(0, weight=1)

        log_frame = tk.Frame(card, bg=BORDER, highlightthickness=0, bd=0)
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log = tk.Text(
            log_frame, bg="#0e1015", fg=TEXT, insertbackground=TEXT,
            font=("Consolas", 9), wrap="word", relief="flat", padx=8, pady=6,
            state="disabled", height=10,
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        for name, color in LOG_TAG_COLORS.items():
            self.log.tag_configure(name, foreground=color)
        self.log.tag_configure("PLAIN", foreground=TEXT)
        self.log.tag_configure("PROMPT", foreground=WARN, font=("Consolas", 9, "bold"))

        prompt_box = ttk.Frame(card, style="Card.TFrame")
        prompt_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        prompt_box.columnconfigure(0, weight=1)

        self.prompt_var = tk.StringVar(value="Tidak ada langkah manual yang menunggu.")
        self.prompt_label = ttk.Label(prompt_box, textvariable=self.prompt_var,
                                      style="Muted.TLabel", wraplength=400,
                                      justify="left")
        self.prompt_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

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
        right.grid(row=0, column=1, sticky="nsew")
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

        # Frame inilah yang jadi induk jendela Chromium (lihat win_embed.embed).
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

    # -------------------------------------------------------------- helpers

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

    # ------------------------------------------------------- input dari GUI

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

    # ------------------------------------------------------------ menjalankan

    def _collect_inputs(self):
        """Validasi form. Return (url, error_message)."""
        raw_video = self.video_var.get()
        email = self.email_var.get().strip()
        tiktok_password = self.tiktok_password_var.get()
        app_password = self.app_password_var.get().strip()
        inbox = self.inbox_var.get().strip() or email

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

    def _start(self):
        if self.worker and self.worker.is_alive():
            return

        url, error = self._collect_inputs()
        if error:
            messagebox.showwarning("Belum bisa dijalankan", error, parent=self.root)
            return

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
        self.stop_button.configure(state="normal")
        self._set_status("Menjalankan...")
        self._log_line("=" * 58)
        self._log_line(f"[GUI] Target: {url}")

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
        if not (self.worker and self.worker.is_alive()):
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
        builtins.input = self._original_input
        self.run_button.configure(state="normal")
        self.reset_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._set_prompt_idle()
        self._release_embedded()
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        if self.stopping:
            self._set_status("Dihentikan")
        else:
            self._set_status("Laporan terkirim" if ok else "Selesai - belum terkonfirmasi")

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
        confirm = messagebox.askyesno(
            "Hapus sesi login",
            f"Hapus {config.STORAGE_STATE_PATH}?\n\n"
            "Run berikutnya wajib login penuh, dan login baru berulang adalah "
            "pemicu utama munculnya OTP serta captcha.",
            parent=self.root,
        )
        if confirm:
            clear_session_file("dihapus dari GUI")

    # ------------------------------------------------------------- ui queue

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

    # --------------------------------------------------------------- penutup

    def _on_close(self):
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
