"""The app's Transcription tab: transcribe a course's lecture recordings on the BGU cluster.

Finds every .mp4 (or, with no video, .m4a or .mp3) under the
chosen folder with no .srt of the same name beside it, makes an mp3 of it if missing (ffmpeg), runs the Hebrew Whisper GPU job on the cluster
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

HOST = "bgu-login"  # ~/.ssh/config alias for the cluster's login node, with its key
# Fail instead of waiting on a password prompt nobody sees, or on a VPN that's down.
SSH = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
       # and drop a link that went dead mid-transfer within a minute, so the retry below takes over
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
PARALLEL = 3  # lectures in flight at once; each holds one GPU job while it runs
POLL = 30  # seconds between checks on a running job
RETRY, GIVE_UP = 30, 30 * 60  # seconds between tries while the connection is down, and how long to keep trying
WARN_EVERY = 3 * 60  # seconds between "connection lost" popups
# How far a lecture got on the cluster, as its first line: done | failed (then the
# log's end) | running (then submit.sh's last line, then the GPU and progress once the job has started) |
# stale (submit.sh died with no exit code: no job after 10 min, or its job ended 10 min ago; then the log's end) |
# uploaded | partial <bytes uploaded>.
# The job writes its .srt/.txt as it goes, so only submit.sh's exit code says they're whole.
STATE = """cd ~/transcription; s={0}
j=$(grep -o 'job [0-9]*' logs/$s.submit 2>/dev/null | tail -n 1 | cut -d' ' -f2)
gpu=$(grep -m 1 -o 'NVIDIA[^,]*' logs/whisper_he_$j.out 2>/dev/null)  # the job logs it as it starts
if [ -f logs/$s.rc ]; then
  if [ "$(cat logs/$s.rc)" = 0 ] && [ -f output/$s.srt ] && [ -f output/$s.txt ]; then
    echo done; echo "$gpu"  # the GPU, then the job's run time
    echo "$(sacct -j "$j" -X -n -o Elapsed 2>/dev/null | xargs)"
  else echo failed; tail -n 5 logs/$s.submit; fi
elif [ -f logs/$s.submit ] && find logs/$s.submit -mmin +10 | grep -q . && {{ [ -z "$j" ] ||
     sacct -j "$j" -X -n -o State 2>/dev/null | grep -qE 'COMPLETED|FAILED|CANCELLED|TIMEOUT|OUT_OF_ME|NODE_FAIL|PREEMPTED|BOOT_FAIL|DEADLINE'; }}
then echo stale; tail -n 5 logs/$s.submit
elif [ -f logs/$s.submit ]; then echo running; tail -n 1 logs/$s.submit; echo "$gpu"
  # The audio's length in minutes, then how far the job got, as hh:mm:ss.
  grep -o 'audio duration: [0-9.]*' logs/whisper_he_$j.out 2>/dev/null | cut -d' ' -f3
  grep -o '^\\[[0-9:]*' logs/whisper_he_$j.out 2>/dev/null | tail -n 1 | tr -d '['
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
    """Recordings under `folder` with no .srt of the same name beside them, one per lecture."""
    return sorted((p for p in folder.rglob("*.*")
                   if p.suffix.lower() in SOURCES and not p.stem.endswith(".part")  # a half-made mp3
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


def remote_stem(video: Path) -> str:
    """The lecture's name on the cluster: named after the video (path and size), so a later run finds it there.
    Shell-safe ASCII, so a name like "lecture 4.1" or a Hebrew one works in the cluster commands."""
    tag = hashlib.sha1(f"{video.resolve()}|{video.stat().st_size}".encode()).hexdigest()[:10]
    return f"{re.sub(r'[^A-Za-z0-9._-]', '_', video.stem)}_{tag}"


def progress(minutes: str, at: str) -> float:
    """How far the job got: `at` (the log's last timestamp, hh:mm:ss or mm:ss) over the audio's `minutes`.
    0 for anything it can't read, so an odd log line never fails a running lecture."""
    try:
        return sum(float(x) * 60 ** k for k, x in enumerate(reversed(at.split(":")))) / 60 / float(minutes)
    except (ValueError, ZeroDivisionError):
        return 0.0


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
            messagebox.showinfo("Lecture Transcriber", "Every .mp4/.m4a/.mp3 in this course already has an .srt."
                                if self.course.get() else "Choose a course first.")
            return
        self.running = True
        self.stop_event.clear()
        self.current: dict[int, str] = {}
        self.rows = {i: ["Waiting", 0.0, time.monotonic()] for i in range(len(self.videos))}
        self.errors: dict[str, str] = {}
        self.warned = -WARN_EVERY  # when the last "connection lost" popup went up
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
                try:
                    self._run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
                               "-vn", "-ac", "1", "-b:a", "64k", str(part)])
                    part.replace(mp3)
                finally:
                    part.unlink(missing_ok=True)
            stem = remote_stem(video)
            state, detail = self._state(i, stem)
            if state.startswith("partial"):
                self._step(i, "Uploading")
                have, size = int(state.split()[1]), mp3.stat().st_size
                if have != size:
                    # From an ASCII-named copy: sftp's batch parsing and globbing never see the real name
                    # (Hebrew, spaces, "&", "[ ]"). reput carries on a half-uploaded file; put starts over.
                    with tempfile.TemporaryDirectory() as tmp:
                        shutil.copyfile(mp3, Path(tmp, stem + ".mp3"))
                        batch = f"-mkdir audio\n{'reput' if 0 < have < size else 'put'} {stem}.mp3 audio/{stem}.mp3.part\n"
                        self._remote(i, ["sftp", *SSH, "-b", "-", HOST], batch, cwd=Path(tmp))
                    state, detail = self._state(i, stem)
                    if state.startswith("partial") and (have := int(state.split()[1])) != size:
                        # START would make a short file the lecture's audio
                        raise RuntimeError(f"Upload incomplete: {have} of {size} bytes reached the cluster. "
                                           "Press Run to resume.")
            started = False
            while state != "done":
                if started and state != "running":
                    if state == "failed":
                        raise RuntimeError(detail or "The job failed. See ~/transcription/logs/ on the cluster.")
                    raise RuntimeError("submit.sh stopped without an exit code:\n" + detail if state == "stale"
                                       else f"The job didn't start (the cluster says: {state}).")
                if state != "running":  # not started yet, or failed or died on an earlier run
                    self._remote(i, ["ssh", *SSH, HOST, START.format(stem)])
                    started = True
                    state, detail = self._state(i, stem)  # not the old run's log as this one's progress
                    continue
                _, gpu, minutes, at = (detail.split("\n") + [""] * 3)[:4]
                gpu = gpu.strip().replace("NVIDIA GeForce ", "")
                step = ("Transcribing on the GPU" if "RUNNING" in detail else "Waiting for a GPU") \
                    + (f" ({gpu})" if gpu else "")
                self._step(i, step, progress(minutes, at.strip()))
                self._wait(POLL)
                state, detail = self._state(i, stem)
            gpu, _, took = detail.partition("\n")
            done = ["Done", *filter(None, (gpu.replace("NVIDIA GeForce ", ""), took.strip().removeprefix("00:")))]
            self._step(i, "Downloading")
            with tempfile.TemporaryDirectory() as tmp:
                # The two whole files by name, never a glob that could catch a job's .part files.
                self._remote(i, ["scp", *SSH, f"{HOST}:transcription/output/{stem}.srt",
                                 f"{HOST}:transcription/output/{stem}.txt", tmp])
                txt = transcripts_dir(video) / f"{video.stem}.txt"
                if txt.exists():  # keep a transcript that's already there (maybe from another tool)
                    done.append("kept the existing .txt")
                else:
                    shutil.move(Path(tmp, stem + ".txt"), txt)
                shutil.move(Path(tmp, stem + ".srt"), video.with_suffix(".srt"))  # last: marks the lecture done
            self.events.put((i, "  ·  ".join(done), 1.0))
        except Stopped:
            self.events.put((i, "Stopped", None))
        except Exception as e:  # one lecture failing shouldn't stop the others
            self.errors[str(video)] = str(e)
            self.events.put((i, "Failed", None))

    def _step(self, i: int, step: str, done: float = 0.0) -> None:
        """`done`: how much of the step is behind it, moving the bar toward the next step."""
        self.current[i] = step
        start = STEPS[step.split(" (")[0]]  # "Transcribing on the GPU (RTX 3090)"
        end = min((v for v in STEPS.values() if v > start), default=1.0)
        self.events.put((i, step, start + (end - start) * min(done, 1.0)))

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
            t0 = self.rows[i][2] if status == self.rows[i][0] else time.monotonic()  # the bar moved, the step didn't
            self.rows[i] = [status, self.rows[i][1] if fraction is None else fraction, t0]
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
            return
        if any(r[0].startswith("Connection lost") for r in self.rows.values()) and now - self.warned > WARN_EVERY:
            messagebox.showwarning("Lecture Transcriber", "Lost the connection to the cluster. Check the VPN; "
                                   f"the app keeps retrying for {GIVE_UP // 60} min.")
            self.warned = time.monotonic()  # counted from when it's closed, so they don't pile up
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
