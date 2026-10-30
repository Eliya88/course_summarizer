"""Desktop window: transcribe a folder's lecture recordings on the BGU cluster.

Finds every L<n>.mp4 under the chosen folder with no L<n>.srt beside it,
makes an mp3 of it (ffmpeg), runs the Hebrew Whisper GPU job on the cluster
(~/transcription/submit.sh) and brings back the .srt, next to the
video, and the .txt, into the Transcripts folder beside it, where main.py
picks it up.

Launch with "Transcriber.bat" (no console) or `python transcriber.py`.
"""
from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from app import ACCENT, BG, BORDER, CARD, FONT, GREEN, MUTED, RED, ROOT, TEXT, _card, _style
from src.config import _TRANSCRIPT_DIR_NAMES, _natural_key

HOST = "bgu-login"  # ~/.ssh/config alias for slurm.bgu.ac.il, with the cluster key
SSH = ["-o", "BatchMode=yes"]  # fail instead of waiting on a password prompt nobody sees
PARALLEL = 3  # lectures in flight at once; each holds one GPU job while it runs
SETTINGS = ROOT / "output" / "transcriber_settings.json"
LECTURE = re.compile(r"L\d+", re.I)
# Submits the job (an RTX 3090 if one frees up within a minute, else a GTX 1080),
# waits for it and prints the end of its log; exits non-zero if it failed.
JOB = "bash ~/transcription/submit.sh ~/audio/{0}.mp3"
# Where each step starts on its lecture's share of the bar.
STEPS = {"Converting to mp3": 0.0, "Uploading": 0.1, "Transcribing on the GPU": 0.25, "Downloading": 0.95}


class Stopped(Exception):
    pass


def pending(folder: Path) -> list[Path]:
    """L<n>.mp4 files under `folder` with no .srt of the same name beside them."""
    return sorted((p for p in folder.rglob("*.mp4")
                   if LECTURE.fullmatch(p.stem) and not p.with_suffix(".srt").exists()),
                  key=lambda p: _natural_key(str(p)))


def transcripts_dir(video: Path) -> Path:
    """The video folder's Transcripts subfolder (any name main.py accepts), made if missing."""
    found = next((d for d in video.parent.iterdir() if d.is_dir() and d.name.lower() in _TRANSCRIPT_DIR_NAMES),
                 None)
    if found:
        return found
    (d := video.parent / "Transcripts").mkdir(exist_ok=True)
    return d


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Lecture Transcriber")
        self.minsize(680, 0)
        self.configure(background=BG)
        _style(self)
        px = float(self.tk.call("tk", "scaling"))
        s = ttk.Style(self)
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                    bordercolor=BORDER, rowheight=int(px * 18))
        s.configure("Treeview.Heading", background=BG, foreground=MUTED, font=(FONT, 8, "bold"), relief="flat")
        s.map("Treeview", background=[("selected", BG)], foreground=[("selected", TEXT)])

        self.events: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.procs: set[subprocess.Popen] = set()
        self.running = self.stopping = False
        self.videos: list[Path] = []
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if not saved.get("base"):  # first launch: start from the summarizer's base folder
            try:
                saved = {"base": json.loads((ROOT / "output" / "app_settings.json").read_text(encoding="utf-8"))["base"]}
            except (OSError, ValueError, KeyError):
                pass
        self.base = tk.StringVar(value=saved.get("base", ""))
        self.course = tk.StringVar(value=saved.get("course", ""))

        page = ttk.Frame(self, style="Root.TFrame", padding=(20, 16, 20, 20))
        page.pack(fill="both", expand=True)
        ttk.Label(page, text="Lecture Transcriber", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="Transcribe every lecture video (L1.mp4, L2.mp4, …) that has no subtitles yet.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(0, 14))

        # Same layout as the summarizer: a base folder, then a course in it picked from the list.
        source = _card(page, "Source")
        ttk.Label(source, text="BASE FOLDER", style="Field.TLabel").pack(anchor="w")
        row = ttk.Frame(source)
        row.pack(fill="x", pady=(2, 12))
        ttk.Entry(row, textvariable=self.base).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._browse).pack(side="left", padx=(8, 0))
        ttk.Label(source, text="COURSE NAME", style="Field.TLabel").pack(anchor="w")
        row = ttk.Frame(source)
        row.pack(fill="x", pady=(2, 0))
        prefix = ttk.Label(row, style="Muted.TLabel")
        prefix.pack(side="left")
        self.base.trace_add("write", lambda *_: prefix.configure(text=self._prefix()))
        prefix.configure(text=self._prefix())
        courses = ttk.Combobox(row, textvariable=self.course, state="readonly")
        courses.configure(postcommand=lambda: courses.configure(values=self._courses()))
        courses.bind("<<ComboboxSelected>>", lambda _: self._scan())
        courses.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Button(row, text="Scan", command=self._scan).pack(side="left", padx=(8, 0))

        lectures = _card(page, "Lectures")
        self.tree = ttk.Treeview(lectures, columns=("lecture", "status"), show="headings", height=8)
        self.tree.heading("lecture", text="LECTURE", anchor="w")
        self.tree.heading("status", text="STATUS", anchor="w")
        self.tree.column("lecture", width=int(px * 260))
        self.tree.column("status", width=int(px * 160))
        self.tree.pack(fill="x")

        actions = ttk.Frame(page, style="Root.TFrame")
        actions.pack(fill="x", pady=(4, 8))
        self.run_btn = ttk.Button(actions, text="▶  Run", style="Accent.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
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

        self._scan()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.update_idletasks()
        w, h = max(self.winfo_reqwidth(), 680), self.winfo_reqheight()
        self.geometry(f"+{(self.winfo_screenwidth() - w) // 2}+{max(0, (self.winfo_screenheight() - h) // 2 - 30)}")

    def _set_status(self, text: str, color: str) -> None:
        self.status.configure(text=f"●  {text}", foreground=color)

    def _browse(self) -> None:
        path = filedialog.askdirectory(initialdir=self.base.get() or None)
        if path:
            self.base.set(str(Path(path)))
            self.course.set("")
            self._scan()

    def _prefix(self) -> str:
        return str(Path(self.base.get())).rstrip("\\") + "\\" if self.base.get() else ""

    def _courses(self) -> list[str]:
        try:
            return sorted(d.name for d in Path(self.base.get()).iterdir()
                          if d.is_dir() and not d.name.startswith((".", "_")))
        except OSError:
            return []

    def _scan(self) -> None:
        if self.running:  # the list is the run's progress; keep it until the run ends
            return
        folder = Path(self.base.get(), self.course.get())
        self.tree.delete(*self.tree.get_children())
        ready = bool(self.base.get() and self.course.get() and folder.is_dir())
        self.videos = pending(folder) if ready else []
        for i, video in enumerate(self.videos):
            self.tree.insert("", "end", iid=str(i), values=(video.relative_to(folder), "Waiting"))
        self.stage.configure(text=f"{len(self.videos)} lecture(s) to transcribe" if ready else "Choose a course")
        SETTINGS.parent.mkdir(exist_ok=True)
        SETTINGS.write_text(json.dumps({"base": self.base.get(), "course": self.course.get()}), encoding="utf-8")

    def start(self) -> None:
        self._scan()
        if not self.videos:
            messagebox.showinfo("Lecture Transcriber", "Every L<n>.mp4 in this course already has an .srt."
                                if self.course.get() else "Choose a course first.")
            return
        self.running, self.stopping = True, False
        self.rows = {i: ["Waiting", 0.0, time.monotonic()] for i in range(len(self.videos))}
        self.errors: dict[str, str] = {}
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("Running…", ACCENT)
        threading.Thread(target=self._run_all, daemon=True).start()
        self.after(200, self._tick)

    def _run_all(self) -> None:
        with ThreadPoolExecutor(PARALLEL) as pool:
            list(pool.map(self._transcribe, range(len(self.videos))))
        self.events.put(None)

    def _transcribe(self, i: int) -> None:
        video = self.videos[i]
        try:
            mp3 = video.with_suffix(".mp3")
            if not mp3.exists():
                self._step(i, "Converting to mp3")
                part = mp3.with_name(mp3.stem + ".part.mp3")  # a stopped run leaves no half mp3 behind
                self._run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                           "-vn", "-ac", "1", "-b:a", "64k", str(part)])
                part.replace(mp3)
            # Remote names are ASCII and unique, so parallel lectures never collide.
            stem = f"{video.stem}_{datetime.now():%Y%m%d_%H%M%S}_{i}"
            self._step(i, "Uploading")
            self._run(["scp", *SSH, str(mp3), f"{HOST}:audio/{stem}.mp3"])
            self._step(i, "Transcribing on the GPU")
            self._run(["ssh", *SSH, HOST, JOB.format(stem)])
            self._step(i, "Downloading")
            with tempfile.TemporaryDirectory() as tmp:
                self._run(["scp", *SSH, f"{HOST}:transcription/output/{stem}.*", tmp])
                txt = transcripts_dir(video) / f"{video.stem}.txt"
                if not txt.exists():  # keep a transcript that's already there (maybe from another tool)
                    shutil.move(Path(tmp, stem + ".txt"), txt)
                shutil.move(Path(tmp, stem + ".srt"), video.with_suffix(".srt"))  # last: marks the lecture done
            self.events.put((i, "Done", 1.0))
        except Stopped:
            self.events.put((i, "Stopped", None))
        except Exception as e:  # one lecture failing shouldn't stop the others
            self.errors[str(video)] = str(e)
            self.events.put((i, "Failed", None))

    def _step(self, i: int, step: str) -> None:
        self.events.put((i, step, STEPS[step]))

    def _run(self, cmd: list[str]) -> str:
        with self.lock:
            if self.stopping:
                raise Stopped
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.procs.add(proc)
        out = proc.communicate()[0]
        with self.lock:
            self.procs.discard(proc)
        if self.stopping:
            raise Stopped
        if proc.returncode:
            raise RuntimeError(out.strip()[-600:] or f"{cmd[0]} exited with code {proc.returncode}")
        return out

    def _tick(self) -> None:
        finished = False
        while not self.events.empty():
            event = self.events.get()
            if event is None:
                finished = True
                continue
            i, status, fraction = event
            self.rows[i] = [status, self.rows[i][1] if fraction is None else fraction, time.monotonic()]
        now = time.monotonic()
        for i, (status, _, t0) in self.rows.items():
            elapsed = int(now - t0)
            text = f"{status}  ·  {elapsed // 60}:{elapsed % 60:02d}" if status in STEPS else status
            self.tree.set(str(i), "status", text)
        value = sum(r[1] for r in self.rows.values()) / len(self.rows)
        self.progress.configure(value=value * 100)
        self.percent.configure(text=f"{int(value * 100)}%")
        done = sum(r[0] == "Done" for r in self.rows.values())
        self.stage.configure(text=f"{done} of {len(self.rows)} transcribed")
        if finished:
            self._finished()
        else:
            self.after(500, self._tick)

    def _finished(self) -> None:
        self.running = False
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if self.stopping:
            self._set_status("Stopped", MUTED)
        elif self.errors:
            self._set_status(f"{len(self.errors)} failed", RED)
            messagebox.showerror("Lecture Transcriber", "\n\n".join(f"{v}:\n{e}" for v, e in self.errors.items()))
        else:
            self._set_status("Done", GREEN)

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            for proc in self.procs:
                proc.kill()

    def _close(self) -> None:
        if self.running and not messagebox.askyesno("Lecture Transcriber", "A run is in progress. Stop it and quit?"):
            return
        self.stop()
        self.destroy()


if __name__ == "__main__":
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    App().mainloop()
