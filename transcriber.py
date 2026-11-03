"""The app's Transcription tab: transcribe a course's lecture recordings on the BGU cluster.

Finds every L<n>.mp4 (or, with no video, L<n>.m4a or L<n>.mp3) under the
chosen folder with no L<n>.srt beside it, makes an mp3 of it if missing (ffmpeg), runs the Hebrew Whisper GPU job on the cluster
(~/transcription/submit.sh) and brings back the .srt, next to the
video, and the .txt, into the Transcripts folder beside it, where main.py
picks it up.

The VPN drops now and then, so every step can pick up where it stopped: each
lecture has a fixed name on the cluster, the job runs detached from the
connection, and a lecture's next step is read off the cluster's files. A lost
connection is retried for a while; after that, Run again resumes.

Open it from "Course Summarizer.bat" or `python app.py`.
"""
from __future__ import annotations

import hashlib
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk

from app import (ACCENT, BG, BORDER, CARD, FONT, GREEN, MUTED, RED, SETTINGS, TEXT, Page, _card, _load_settings,
                 _save_settings)
from src.config import _TRANSCRIPT_DIR_NAMES, _natural_key

HOST = "bgu-login"  # ~/.ssh/config alias for slurm.bgu.ac.il, with the cluster key
# Fail instead of waiting on a password prompt nobody sees, or on a VPN that's down.
SSH = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
PARALLEL = 3  # lectures in flight at once; each holds one GPU job while it runs
LECTURE = re.compile(r"L\d+", re.I)
POLL = 30  # seconds between checks on a running job
RETRY, GIVE_UP = 30, 30 * 60  # seconds between tries while the connection is down, and how long to keep trying
# How far a lecture got on the cluster, as its first line: done | failed (then the
# log's end) | running (then submit.sh's last line) | uploaded | partial <bytes uploaded>.
# The job writes its .srt/.txt as it goes, so only submit.sh's exit code says they're whole.
STATE = """cd ~/transcription; s={0}
if [ -f logs/$s.rc ]; then
  if [ "$(cat logs/$s.rc)" = 0 ] && [ -f output/$s.srt ] && [ -f output/$s.txt ]; then
    echo done; j=$(grep -o 'job [0-9]*' logs/$s.submit | tail -n 1 | cut -d' ' -f2)
    echo "$(grep -m 1 -o 'NVIDIA[^,]*' logs/whisper_he_$j.out 2>/dev/null)"  # the GPU, then the job's run time
    echo "$(sacct -j "$j" -X -n -o Elapsed 2>/dev/null | xargs)"
  else echo failed; tail -n 5 logs/$s.submit; fi
elif [ -f logs/$s.submit ]; then echo running; tail -n 1 logs/$s.submit
elif [ -f ~/audio/$s.mp3 ]; then echo uploaded
else echo partial $(stat -c %s ~/audio/$s.mp3.part 2>/dev/null || echo 0); fi"""
# Starts submit.sh detached, so a dropped connection doesn't kill it. It takes an
# RTX 3090 if one frees up within a minute, else a GTX 1080, and waits for the
# job; its exit code lands in logs/<stem>.rc.
START = r"""s={0}; cd ~/transcription; mv -f ~/audio/$s.mp3.part ~/audio/$s.mp3 2>/dev/null
rm -f logs/$s.rc; : > logs/$s.submit
setsid nohup bash -c "bash submit.sh ~/audio/$s.mp3 > logs/$s.submit 2>&1; echo \$? > logs/$s.rc.tmp; mv logs/$s.rc.tmp logs/$s.rc" \
  > /dev/null 2>&1 < /dev/null &"""
# Where each step starts on its lecture's share of the bar.
STEPS = {"Converting to mp3": 0.0, "Uploading": 0.1, "Waiting for a GPU": 0.25, "Transcribing on the GPU": 0.3,
         "Downloading": 0.95}


class Stopped(Exception):
    pass


SOURCES = (".mp4", ".m4a", ".mp3")  # a lecture with several of these is listed by the first


def pending(folder: Path) -> list[Path]:
    """L<n> recordings under `folder` with no .srt of the same name beside them, one per lecture."""
    return sorted((p for p in folder.rglob("*.*")
                   if p.suffix.lower() in SOURCES and LECTURE.fullmatch(p.stem)
                   and not p.with_suffix(".srt").exists()
                   and not any(p.with_suffix(s).exists() for s in SOURCES[:SOURCES.index(p.suffix.lower())])),
                  key=lambda p: _natural_key(str(p)))


def transcripts_dir(video: Path) -> Path:
    """The video folder's Transcripts subfolder (any name main.py accepts), made if missing."""
    found = next((d for d in video.parent.iterdir() if d.is_dir() and d.name.lower() in _TRANSCRIPT_DIR_NAMES),
                 None)
    if found:
        return found
    (d := video.parent / "Transcripts").mkdir(exist_ok=True)
    return d


class TranscriberPage(Page):
    def __init__(self, window) -> None:
        super().__init__(window, "Transcribe every lecture recording (L1.mp4, L2.m4a, L3.mp3, …) "
                                 "that has no subtitles yet.")
        px = float(self.tk.call("tk", "scaling"))
        s = ttk.Style(self)
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                    bordercolor=BORDER, rowheight=int(px * 18))
        s.configure("Treeview.Heading", background=BG, foreground=MUTED, font=(FONT, 8, "bold"), relief="flat")
        s.map("Treeview", background=[("selected", BG)], foreground=[("selected", TEXT)])

        self.events: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.procs: set[subprocess.Popen] = set()
        self.running = False
        self.stop_event = threading.Event()
        self.videos: list[Path] = []
        self.course = tk.StringVar(value=_load_settings(SETTINGS).get("transcribe_course", ""))

        # A course in the shared base folder, picked from the list; a new base folder clears it.
        source = _card(self, "Source")
        self.base.trace_add("write", lambda *_: (self.course.set(""), self._scan()))
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

        lectures = _card(self, "Lectures")
        self.tree = ttk.Treeview(lectures, columns=("lecture", "status"), show="headings", height=8)
        self.tree.heading("lecture", text="LECTURE", anchor="w")
        self.tree.heading("status", text="STATUS", anchor="w")
        self.tree.column("lecture", width=int(px * 260))
        self.tree.column("status", width=int(px * 200))
        self.tree.pack(fill="x")

        actions = ttk.Frame(self, style="Root.TFrame")
        actions.pack(fill="x", pady=(4, 8))
        self.run_btn = ttk.Button(actions, text="▶  Run", style="Accent.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.status = ttk.Label(actions, style="Root.TLabel")
        self.status.pack(side="right")
        self._set_status("Ready", MUTED)

        self._progress_bar()
        self._scan()

    def busy(self) -> bool:
        return self.running

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
        _save_settings(transcribe_course=self.course.get())

    def start(self) -> None:
        self._scan()
        if not self.videos:
            messagebox.showinfo("Lecture Transcriber", "Every L<n>.mp4/.m4a/.mp3 in this course already has an .srt."
                                if self.course.get() else "Choose a course first.")
            return
        self.running = True
        self.stop_event.clear()
        self.current: dict[int, str] = {}
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
            # Named after the video (path and size), so a later run finds this lecture on the cluster.
            stem = f"{video.stem}_{hashlib.sha1(f'{video.resolve()}|{video.stat().st_size}'.encode()).hexdigest()[:10]}"
            state, detail = self._state(i, stem)
            if state.startswith("partial"):
                self._step(i, "Uploading")
                have, size = int(state.split()[1]), mp3.stat().st_size
                if have < size:  # sftp's reput carries on a half-uploaded file
                    batch = f"{'reput' if have else 'put'} {mp3.name} audio/{stem}.mp3.part\n"
                    self._remote(i, ["sftp", *SSH, "-b", "-", HOST], batch, cwd=mp3.parent)
            started = False
            while state != "done":
                if state == "failed" and started:
                    raise RuntimeError(detail or "The job failed. See ~/transcription/logs/ on the cluster.")
                if state != "running":  # not started yet, or failed on an earlier run
                    self._remote(i, ["ssh", *SSH, HOST, START.format(stem)])
                    started = True
                step = "Transcribing on the GPU" if "RUNNING" in detail else "Waiting for a GPU"
                if self.current.get(i) != step:
                    self._step(i, step)
                self._wait(POLL)
                state, detail = self._state(i, stem)
            gpu, _, took = detail.partition("\n")
            done = "  ·  ".join(["Done", *filter(None, (gpu.replace("NVIDIA GeForce ", ""),
                                                        took.strip().removeprefix("00:")))])
            self._step(i, "Downloading")
            with tempfile.TemporaryDirectory() as tmp:
                self._remote(i, ["scp", *SSH, f"{HOST}:transcription/output/{stem}.*", tmp])
                txt = transcripts_dir(video) / f"{video.stem}.txt"
                if not txt.exists():  # keep a transcript that's already there (maybe from another tool)
                    shutil.move(Path(tmp, stem + ".txt"), txt)
                shutil.move(Path(tmp, stem + ".srt"), video.with_suffix(".srt"))  # last: marks the lecture done
            self.events.put((i, done, 1.0))
        except Stopped:
            self.events.put((i, "Stopped", None))
        except Exception as e:  # one lecture failing shouldn't stop the others
            self.errors[str(video)] = str(e)
            self.events.put((i, "Failed", None))

    def _step(self, i: int, step: str) -> None:
        self.current[i] = step
        self.events.put((i, step, STEPS[step]))

    def _state(self, i: int, stem: str) -> tuple[str, str]:
        state, _, detail = self._remote(i, ["ssh", *SSH, HOST, STATE.format(stem)]).strip().partition("\n")
        return state, detail.strip()

    def _wait(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            raise Stopped

    def _remote(self, i: int, cmd: list[str], stdin: str | None = None, cwd: Path | None = None) -> str:
        """`_run` for a command that talks to the cluster: retried while the connection is down."""
        failing_since = None
        while True:
            try:
                out = self._run(cmd, stdin, cwd)
                if failing_since is not None and i in self.current:
                    self._step(i, self.current[i])  # back from "Connection lost"
                return out
            except RuntimeError as e:
                failing_since = failing_since or time.monotonic()
                if time.monotonic() - failing_since > GIVE_UP:
                    raise RuntimeError(f"No connection to the cluster for {GIVE_UP // 60} min "
                                       f"(press Run to resume):\n{e}") from None
                self.events.put((i, "Connection lost, retrying", None))
                self._wait(RETRY)

    def _run(self, cmd: list[str], stdin: str | None = None, cwd: Path | None = None) -> str:
        with self.lock:
            if self.stop_event.is_set():
                raise Stopped
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                                    encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.procs.add(proc)
        out = proc.communicate(stdin)[0]
        with self.lock:
            self.procs.discard(proc)
        if self.stop_event.is_set():
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
            text = status if status.startswith(("Waiting", "Done", "Failed", "Stopped")) else \
                f"{status}  ·  {elapsed // 60}:{elapsed % 60:02d}"
            self.tree.set(str(i), "status", text)
        value = sum(r[1] for r in self.rows.values()) / len(self.rows)
        self.progress.configure(value=value * 100)
        self.percent.configure(text=f"{int(value * 100)}%")
        done = sum(r[0].startswith("Done") for r in self.rows.values())
        self.stage.configure(text=f"{done} of {len(self.rows)} transcribed")
        if finished:
            self._finished()
        else:
            self.after(500, self._tick)

    def _finished(self) -> None:
        self.running = False
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if self.stop_event.is_set():
            self._set_status("Stopped", MUTED)
        elif self.errors:
            self._set_status(f"{len(self.errors)} failed", RED)
            messagebox.showerror("Lecture Transcriber", "\n\n".join(f"{v}:\n{e}" for v, e in self.errors.items()))
        else:
            self._set_status("Done", GREEN)

    def stop(self) -> None:
        with self.lock:
            self.stop_event.set()
            for proc in self.procs:
                proc.kill()
