import re

from transcriber import START, STATE, pending, progress, remote_stem, transcripts_dir


def test_pending_finds_lecture_recordings_without_subtitles(tmp_path):
    rec = tmp_path / "Course" / "Recordings"
    rec.mkdir(parents=True)
    for name in ("L1.mp4", "L1.srt", "L2.mp4", "L2.mp3", "L10.mp4", "lecture 3.mp4", "L4.mp3",
                 "L5.m4a", "L5.mp3", "L6.mp4", "L6.m4a", "L7.1.mp4", "L7.2.mp4", "L7.2.srt",
                 "L8.part.mp3"):
        (rec / name).touch()
    assert [p.name for p in pending(tmp_path)] == ["L2.mp4", "L4.mp3", "L5.m4a", "L6.mp4", "L7.1.mp4", "L10.mp4",
                                                          "lecture 3.mp4"]


def test_transcripts_dir_reuses_an_existing_one_or_makes_it(tmp_path):
    video = tmp_path / "L1.mp4"
    assert transcripts_dir(video) == tmp_path / "Transcripts" and (tmp_path / "Transcripts").is_dir()
    (tmp_path / "Transcripts").rmdir()
    (tmp_path / "תמלולים").mkdir()
    assert transcripts_dir(video) == tmp_path / "תמלולים"


def test_remote_stem_is_shell_safe_ascii_and_stable(tmp_path):
    folder = tmp_path / "my course" / "הקלטות"
    folder.mkdir(parents=True)
    for name in ("Q&A.mp3", "Lecture 1. Intro.mp4", "הרצאה 3.mp4", "L [2] it's.m4a"):
        (video := folder / name).write_bytes(b"x")
        stem = remote_stem(video)
        assert re.fullmatch(r"[A-Za-z0-9._-]+_[0-9a-f]{10}", stem), stem
        assert remote_stem(video) == stem
    assert remote_stem(folder / "Q&A.mp3").startswith("Q_A_")
    assert remote_stem(folder / "Lecture 1. Intro.mp4").startswith("Lecture_1._Intro_")


def test_progress_reads_the_log_timestamp_against_the_audio_length():
    assert progress("60", "00:30:00") == 0.5
    assert progress("60", "15:00") == 0.25
    assert progress("", "00:30:00") == progress("60", "") == progress("0", "00:01:00") == progress("60", "x:1") == 0.0


def test_cluster_scripts_fill_in_the_stem():
    # A bash "{ ...; }" left unescaped would make str.format raise on every poll.
    for script in (STATE, START):
        assert "s=L4_0df983b546" in script.format("L4_0df983b546")
