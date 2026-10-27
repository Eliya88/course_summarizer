"""Desktop window for main.py: pick a course or book, tick options, press Run.

Launch with "Course Summarizer.bat" (no console) or `python app.py`.
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).parent
# pythonw has no console of its own; run the child with python.exe so its
# output reaches the pipe, and hide its window instead.
PYTHON = Path(sys.executable).with_name("python.exe")
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
# output/ is gitignored, so the saved base path stays local.
SETTINGS = ROOT / "output" / "app_settings.json"
OUTPUT_RE = re.compile(r"^(?:Copied to|Done\. Wrote): (.+\.pdf)\s*$")

OPTIONS = [
    ("Include exercises", "Also summarize exercise and tutorial units", "--with-exercises"),
    ("Add past-exam practice", "A worked exam question after each chapter", "--with-tests"),
    ("Ignore cache", "Re-run every unit (new API calls)", "--no-cache"),
    ("Rebuild course context", "Re-read the full course digest", "--refresh-context"),
]

BG, CARD, BORDER = "#f3f4f6", "#ffffff", "#e5e7eb"
TEXT, MUTED = "#111827", "#6b7280"
ACCENT, ACCENT_HOVER, ACCENT_OFF = "#2563eb", "#1d4ed8", "#93c5fd"
GREEN, RED = "#16a34a", "#dc2626"
FONT = "Segoe UI"
LOG = ROOT / "output" / "last_run.log"

SETUP, PDF = 0.03, 0.05  # bar shares before the first chapter and after the last
EXAM = 0.15  # share of a chapter's slice for its past-exam question
TAU = 25.0  # seconds; within a step the bar eases toward the step's end on this scale


class Progress:
    """Turns main.py's printed lines into a 0-1 estimate and a short label.

    Every chapter gets an equal slice of the bar. Inside it, each part splits
    into reading+agenda, planning, the topic batches and the glossary, weighted
    roughly by how long those Claude calls take. Between lines the bar eases
    toward the current step's end, so it keeps moving but never overshoots;
    cached chapters jump straight to their end."""

    def __init__(self, with_tests: bool) -> None:
        self.exam = with_tests
        self.units, self.unit = 1, -1
        self.parts, self.part, self.first_topic = 1, 1, None
        self.shown = 0.0
        self._set((0.0, SETUP), "Starting…")

    def _unit(self, lo: float, hi: float) -> tuple[float, float]:
        size = (1 - SETUP - PDF) / self.units
        base = SETUP + size * min(self.unit, self.units - 1)
        return base + size * lo, base + size * hi

    def _part(self, lo: float, hi: float) -> tuple[float, float]:
        summary = 1 - EXAM if self.exam else 1.0
        k = self.part - 1
        return self._unit(summary * (k + lo) / self.parts, summary * (k + hi) / self.parts)

    def _set(self, span: tuple[float, float], label: str, chapter: bool = False) -> None:
        self.span, self.t0 = span, time.monotonic()
        self.label = f"Chapter {self.unit + 1} of {self.units}  ·  {label}" if chapter else label

    def feed(self, line: str) -> None:
        s = line.strip()
        if m := re.match(r"Course: .*units: (\d+)", s):
            self.units = max(1, int(m[1]))
        elif s.startswith("[course_context]"):
            self._set(self.span, "Reading the course")
        elif "no past-exam PDFs were found" in s:
            self.exam = False
        elif s.startswith("[pipeline] Uploading"):
            self._set(self.span, "Uploading past exams")
        elif m := re.match(r"\[pipeline\] (Using cached result for|Processing) unit", s):
            self.unit += 1
            self.parts, self.part = 1, 1
            if m[1].startswith("Using"):
                self._set(self._part(1, 1), "From cache", chapter=True)
        elif m := re.match(r"Part (\d+)/(\d+):", s):
            self.part, self.parts, self.first_topic = int(m[1]), int(m[2]), None
            self._set(self._part(0, 0.15), "Reading the material", chapter=True)
        elif s.startswith("Planning content boundaries"):
            self._set(self._part(0.15, 0.25), "Planning topics", chapter=True)
        elif m := re.match(r"Writing topics (\d+)-(\d+)/(\d+)", s):
            a, b, last = map(int, m.groups())
            if self.first_topic is None:
                self.first_topic = a
            n = last - self.first_topic + 1
            lo, hi = (a - self.first_topic) / n, (b - self.first_topic + 1) / n
            self._set(self._part(0.25 + 0.65 * lo, 0.25 + 0.65 * hi), f"Writing topics {a}–{b}", chapter=True)
        elif s.startswith("Writing glossary"):
            self._set(self._part(0.9, 1), "Writing key terms", chapter=True)
        elif s.startswith("Looking for a past-exam question"):
            self._set(self._unit(1 - EXAM, 1), "Finding an exam question", chapter=True)
        elif s.startswith(("Using cached exam question", "No past-exam question")):
            self.span, self.t0 = self._unit(1, 1), time.monotonic()
        elif s.startswith("Building the PDF"):
            self._set((1 - PDF, 1.0), "Building the PDF")

    def value(self) -> float:
        lo, hi = self.span
        eased = lo + (hi - lo) * (1 - math.exp(-(time.monotonic() - self.t0) / TAU))
        self.shown = max(self.shown, eased)
        return self.shown


def _style(root: tk.Tk) -> None:
    s = ttk.Style(root)
    px = float(root.tk.call("tk", "scaling"))  # pixels per point, so sizes follow display scaling
    s.theme_use("clam")  # the only built-in theme that takes custom colors
    s.configure(".", background=CARD, foreground=TEXT, font=(FONT, 10), bordercolor=BORDER)
    s.configure("Root.TFrame", background=BG)
    s.configure("Root.TLabel", background=BG)
    s.configure("Title.TLabel", background=BG, font=(FONT, 18, "bold"))
    s.configure("Subtitle.TLabel", background=BG, foreground=MUTED)
    s.configure("Section.TLabel", font=(FONT, 11, "bold"))
    s.configure("Muted.TLabel", foreground=MUTED, font=(FONT, 9))
    s.configure("Field.TLabel", foreground=MUTED, font=(FONT, 8, "bold"))
    s.configure("TEntry", padding=6, fieldbackground=CARD, lightcolor=BORDER, darkcolor=BORDER)
    s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
    s.configure("TButton", background=CARD, lightcolor=CARD, darkcolor=CARD,
                focuscolor=CARD, padding=(14, 6))
    s.map("TButton", background=[("active", BG)], foreground=[("disabled", "#9ca3af")])
    s.configure("Accent.TButton", background=ACCENT, foreground="white", font=(FONT, 10, "bold"),
                lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT, focuscolor=ACCENT,
                padding=(22, 7))
    s.map("Accent.TButton", background=[("disabled", ACCENT_OFF), ("active", ACCENT_HOVER)],
          lightcolor=[("disabled", ACCENT_OFF), ("active", ACCENT_HOVER)],
          darkcolor=[("disabled", ACCENT_OFF), ("active", ACCENT_HOVER)],
          bordercolor=[("disabled", ACCENT_OFF)], foreground=[("disabled", "white")])
    s.configure("Seg.Toolbutton", background=BG, padding=(20, 6), anchor="center",
                bordercolor=BORDER, lightcolor=BG, darkcolor=BG, focuscolor=BG)
    s.map("Seg.Toolbutton", background=[("selected", ACCENT), ("active", BORDER)],
          bordercolor=[("selected", ACCENT)], lightcolor=[("selected", ACCENT)],
          darkcolor=[("selected", ACCENT)], focuscolor=[("selected", ACCENT)],
          foreground=[("selected", "white")])
    s.configure("TCheckbutton", font=(FONT, 10, "bold"), indicatorbackground=CARD,
                indicatorsize=int(px * 11), indicatormargin=(0, 0, 8, 0), focuscolor=CARD,
                upperbordercolor=MUTED, lowerbordercolor=MUTED)
    s.map("TCheckbutton", background=[("active", CARD)],
          indicatorbackground=[("selected", ACCENT)], indicatorforeground=[("selected", "white")])
    s.configure("Horizontal.TProgressbar", troughcolor=BORDER, background=ACCENT,
                bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT, thickness=int(px * 6))
    s.configure("Percent.TLabel", background=BG, font=(FONT, 11, "bold"))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Course Summarizer")
        self.minsize(680, 0)
        self.configure(background=BG)
        _style(self)
        px = float(self.tk.call("tk", "scaling"))
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.pdf_path: str | None = None

        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.base = tk.StringVar(value=os.path.normpath(saved["base"]) if saved.get("base") else "")
        self.base.trace_add("write", lambda *_: self._save_settings())
        self.open_when_ready = bool(saved.get("open_when_ready"))
        self.mode = tk.StringVar(value="course")
        self.course_dir = tk.StringVar()
        self.book = tk.StringVar()
        self.chapters = tk.StringVar()
        self.focus = tk.StringVar()
        self.instructions = tk.StringVar()
        self.flags = {flag: tk.BooleanVar() for _, _, flag in OPTIONS}

        page = ttk.Frame(self, style="Root.TFrame", padding=(20, 16, 20, 20))
        page.pack(fill="both", expand=True)

        ttk.Label(page, text="Course Summarizer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Turn a course's lectures, or a textbook's chapters, into one study PDF.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(0, 14))

        # Source card: base folder, then a Course / Book switch and its fields.
        source = self._card(page, "Source")
        ttk.Label(source, text="BASE FOLDER", style="Field.TLabel").pack(anchor="w")
        self._path_field(source, self.base, folder=True).pack(fill="x", pady=(2, 12))

        switch = ttk.Frame(source)
        switch.pack(anchor="w", pady=(0, 12))
        for text, value in (("Course", "course"), ("Book", "book")):
            ttk.Radiobutton(switch, text=text, value=value, variable=self.mode,
                            style="Seg.Toolbutton", command=self._sync_mode).pack(side="left")

        # The course is typed as a name under the base folder, shown as a prefix.
        self.course_panel = ttk.Frame(source)
        ttk.Label(self.course_panel, text="COURSE NAME", style="Field.TLabel").pack(anchor="w")
        row = ttk.Frame(self.course_panel)
        row.pack(fill="x", pady=(2, 0))
        prefix = ttk.Label(row, style="Muted.TLabel")
        prefix.pack(side="left")
        self.base.trace_add("write", lambda *_: prefix.configure(text=self._prefix()))
        prefix.configure(text=self._prefix())
        ttk.Entry(row, textvariable=self.course_dir).pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.book_panel = ttk.Frame(source)
        ttk.Label(self.book_panel, text="BOOK PDF", style="Field.TLabel").pack(anchor="w")
        self._path_field(self.book_panel, self.book).pack(fill="x", pady=(2, 10))
        ttk.Label(self.book_panel, text="CHAPTERS", style="Field.TLabel").pack(anchor="w")
        chapters = ttk.Frame(self.book_panel)
        chapters.pack(fill="x", pady=(2, 10))
        ttk.Entry(chapters, textvariable=self.chapters, width=14).pack(side="left")
        ttk.Label(chapters, text="e.g. 1-5 or 1,3,5", style="Muted.TLabel").pack(side="left", padx=8)
        ttk.Label(self.book_panel, text="FOCUS PDF  ·  OPTIONAL", style="Field.TLabel").pack(anchor="w")
        self._path_field(self.book_panel, self.focus).pack(fill="x", pady=(2, 0))

        options = self._card(page, "Options")
        grid = ttk.Frame(options)
        grid.pack(fill="x")
        grid.columnconfigure((0, 1), weight=1, uniform="opt")
        for i, (text, hint, flag) in enumerate(OPTIONS):
            cell = ttk.Frame(grid)
            cell.grid(row=i // 2, column=i % 2, sticky="w", pady=4)
            ttk.Checkbutton(cell, text=text, variable=self.flags[flag]).pack(anchor="w")
            ttk.Label(cell, text=hint, style="Muted.TLabel").pack(anchor="w", padx=(int(px * 11) + 10, 0))
        ttk.Label(options, text="EXTRA INSTRUCTIONS  ·  OPTIONAL", style="Field.TLabel").pack(anchor="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.instructions).pack(fill="x", pady=(2, 0))

        actions = ttk.Frame(page, style="Root.TFrame")
        actions.pack(fill="x", pady=(4, 8))
        self.run_btn = ttk.Button(actions, text="▶  Run", style="Accent.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(actions, text="Open PDF", state="disabled", command=self._open)
        self.open_btn.pack(side="left")
        self.status = ttk.Label(actions, style="Root.TLabel")
        self.status.pack(side="right")
        self._set_status("Ready", MUTED)

        self.progress = ttk.Progressbar(page, maximum=100)
        self.progress.pack(fill="x", pady=(4, 6))
        info = ttk.Frame(page, style="Root.TFrame")
        info.pack(fill="x")
        self.stage = ttk.Label(info, style="Subtitle.TLabel")
        self.stage.pack(side="left")
        self.percent = ttk.Label(info, style="Percent.TLabel")
        self.percent.pack(side="right")

        self._sync_mode()
        self.bind("<Return>", lambda _: self.run_btn.instate(["!disabled"]) and self.start())
        self.protocol("WM_DELETE_WINDOW", self._close)

        # Open centered on the screen instead of Windows' default top-left spot.
        self.update_idletasks()
        w, h = max(self.winfo_reqwidth(), 680), self.winfo_reqheight()
        self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}+{max(0, (self.winfo_screenheight() - h) // 2 - 30)}")

    def _card(self, parent, title: str) -> ttk.Frame:
        outer = tk.Frame(parent, background=CARD, highlightbackground=BORDER, highlightthickness=1)
        outer.pack(fill="x", pady=(0, 12))
        inner = ttk.Frame(outer, padding=(16, 12, 16, 14))
        inner.pack(fill="x")
        ttk.Label(inner, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        return inner

    def _path_field(self, parent, var, folder=False) -> ttk.Frame:
        frame = ttk.Frame(parent)
        ttk.Entry(frame, textvariable=var).pack(side="left", fill="x", expand=True)

        def browse():
            start = var.get() or self.base.get() or None
            if start and not Path(start).is_dir():
                start = str(Path(start).parent)
            path = (filedialog.askdirectory(initialdir=start) if folder
                    else filedialog.askopenfilename(initialdir=start, filetypes=[("PDF", "*.pdf")]))
            if path:
                var.set(os.path.normpath(path))

        ttk.Button(frame, text="Browse…", command=browse).pack(side="left", padx=(8, 0))
        return frame

    def _set_status(self, text: str, color: str) -> None:
        self.status.configure(text=f"●  {text}", foreground=color)

    def _save_settings(self) -> None:
        SETTINGS.parent.mkdir(exist_ok=True)
        SETTINGS.write_text(json.dumps({"base": self.base.get(), "open_when_ready": self.open_when_ready}),
                            encoding="utf-8")

    def _prefix(self) -> str:
        return os.path.normpath(self.base.get()).rstrip("\\") + "\\" if self.base.get() else ""

    def _sync_mode(self) -> None:
        book = self.mode.get() == "book"
        (self.course_panel if book else self.book_panel).pack_forget()
        (self.book_panel if book else self.course_panel).pack(fill="x")

    def _args(self) -> list[str] | None:
        if self.mode.get() == "course":
            course = Path(self.base.get()) / self.course_dir.get().strip()
            if not self.course_dir.get().strip() or not course.is_dir():
                messagebox.showerror("Course Summarizer", f"No course folder at:\n{course}")
                return None
            args = ["--course-dir", str(course)]
        else:
            if not Path(self.book.get()).is_file():
                messagebox.showerror("Course Summarizer", "Choose the book PDF.")
                return None
            if not self.chapters.get().strip():
                messagebox.showerror("Course Summarizer", "Enter chapters, e.g. 1-5 or 1,3,5.")
                return None
            args = ["--book", self.book.get(), "--chapters", self.chapters.get().strip()]
            if self.focus.get():
                if not Path(self.focus.get()).is_file():
                    messagebox.showerror("Course Summarizer", "The focus PDF doesn't exist.")
                    return None
                args += ["--focus", self.focus.get()]
        if self.instructions.get().strip():
            args += ["--instructions", self.instructions.get().strip()]
        return args + [flag for flag, on in self.flags.items() if on.get()]

    def start(self) -> None:
        args = self._args()
        if args is None:
            return
        self.output: list[str] = []
        self.tracker = Progress(with_tests="--with-tests" in args)
        self.stopped = False
        self.pdf_path = None
        self.open_btn.configure(state="normal", text=self._armed_text())
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("Running…", ACCENT)

        self.proc = subprocess.Popen(
            [str(PYTHON), str(ROOT / "main.py"), *args], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        self.after(100, self._drain)

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.lines.put(line)
        proc.wait()
        self.lines.put(None)

    def _drain(self) -> None:
        while not self.lines.empty():
            line = self.lines.get()
            if line is None:
                self._finished()
                return
            self.output.append(line)
            if m := OUTPUT_RE.match(line):
                self.pdf_path = m.group(1)
            self.tracker.feed(line)
        self._show(self.tracker.value(), self.tracker.label)
        self.after(100, self._drain)

    def _show(self, value: float, label: str) -> None:
        self.progress.configure(value=value * 100)
        self.percent.configure(text=f"{int(value * 100)}%")
        self.stage.configure(text=label)

    def _finished(self) -> None:
        code = self.proc.returncode
        self.proc = None
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        # The full output is kept on disk instead of on screen, for when a run fails.
        LOG.parent.mkdir(exist_ok=True)
        LOG.write_text("".join(self.output), encoding="utf-8")
        self.open_btn.configure(text="Open PDF",
                                state="normal" if code == 0 and self.pdf_path else "disabled")
        if code == 0:
            self._show(1.0, "Your summary is ready")
            self._set_status("Done", GREEN)
            if self.open_when_ready and self.pdf_path:
                os.startfile(self.pdf_path)
        elif self.stopped:
            self._show(self.tracker.shown, "Stopped")
            self._set_status("Stopped", MUTED)
        else:
            self._show(self.tracker.shown, "Failed")
            self._set_status("Failed", RED)
            tail = "".join(self.output[-12:]).strip() or f"Exit code {code}"
            messagebox.showerror("Course Summarizer", f"The run failed:\n\n{tail}\n\nFull output: {LOG}")

    def _open(self) -> None:
        # During a run the button toggles opening the PDF as soon as it's built.
        if self.proc:
            self.open_when_ready = not self.open_when_ready
            self.open_btn.configure(text=self._armed_text())
            self._save_settings()
        else:
            os.startfile(self.pdf_path)

    def _armed_text(self) -> str:
        return "✓  Opens when ready" if self.open_when_ready else "Open when ready"

    def stop(self) -> None:
        if self.proc:
            self.stopped = True
            self.proc.kill()

    def _close(self) -> None:
        if self.proc and not messagebox.askyesno("Course Summarizer", "A run is in progress. Stop it and quit?"):
            return
        self.stop()
        self.destroy()


if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    App().mainloop()
