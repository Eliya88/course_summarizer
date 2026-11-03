from transcriber import pending, transcripts_dir


def test_pending_finds_lecture_recordings_without_subtitles(tmp_path):
    rec = tmp_path / "Course" / "Recordings"
    rec.mkdir(parents=True)
    for name in ("L1.mp4", "L1.srt", "L2.mp4", "L2.mp3", "L10.mp4", "lecture 3.mp4", "L4.mp3",
                 "L5.m4a", "L5.mp3", "L6.mp4", "L6.m4a"):
        (rec / name).touch()
    assert [p.name for p in pending(tmp_path)] == ["L2.mp4", "L4.mp3", "L5.m4a", "L6.mp4", "L10.mp4"]


def test_transcripts_dir_reuses_an_existing_one_or_makes_it(tmp_path):
    video = tmp_path / "L1.mp4"
    assert transcripts_dir(video) == tmp_path / "Transcripts" and (tmp_path / "Transcripts").is_dir()
    (tmp_path / "Transcripts").rmdir()
    (tmp_path / "תמלולים").mkdir()
    assert transcripts_dir(video) == tmp_path / "תמלולים"
