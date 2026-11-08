# -*- coding: utf-8 -*-
import os
import sys

import pytest

from backend import jobs
from backend import scan
from backend import srt as SRT
from backend import tasks


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def _spec(root, book_id, selected, overwrite=False):
    book = os.path.join(root, str(book_id))
    files = []
    for name in scan.list_audio_files(book):
        files.append({
            "filename": name,
            "size": os.path.getsize(os.path.join(book, name)),
            "covered": bool(scan.subtitle_map(book).get(os.path.splitext(name)[0])),
        })
    return {
        "book_id": book_id,
        "title": "Book",
        "audio_root": root,
        "audio_dir": book,
        "files": files,
        "selected": selected,
        "options": dict(jobs.DEFAULT_OPTIONS, overwrite=overwrite),
    }


def test_overwrite_follows_mode():
    assert jobs.overwrite_for_mode("missing") is False
    assert jobs.overwrite_for_mode("all") is True
    assert jobs.overwrite_for_mode("selected") is True
    assert jobs.overwrite_for_mode("missing", "true") is True
    assert jobs.overwrite_for_mode("all", False) is False


def test_resolve_options_clamps_and_parses_bools():
    options = jobs.resolve_options({
        "cpu_threads": 0,
        "beam_size": 999,
        "chunk_seconds": 1,
        "max_chars": 1,
        "max_duration": 999,
        "vad": "false",
        "overwrite": "true",
        "model": "small",
        "zh_script": "bogus",
    })
    assert options["cpu_threads"] == 1
    assert options["beam_size"] == 10
    assert options["chunk_seconds"] == 60
    assert options["max_chars"] == 12
    assert options["max_duration"] == 60.0
    assert options["vad"] is False
    assert options["overwrite"] is True
    assert options["model"] == "small"
    assert options["zh_script"] == "t2s"
    assert jobs.resolve_options({"zh_script": "s2t"})["zh_script"] == "s2t"


def test_job_state_roundtrip_and_normalize(tmp_path):
    base = str(tmp_path / "data")
    state = {
        "book_id": 3,
        "title": "T",
        "status": "running",
        "files": {"a.wav": {"status": "running"}},
        "selected": ["a.wav"],
        "options": {"model": "small"},
    }
    jobs.save_job(base, state)
    loaded = jobs.load_job(base, 3)
    assert loaded["book_id"] == 3
    normalized = jobs.normalize_job(loaded)
    assert normalized["status"] == "interrupted"
    assert "options" not in normalized
    assert jobs.normalize_job(loaded, active=True)["status"] == "running"
    assert jobs.load_job(base, 99) is None


def test_prepare_state_skip_done_and_overwrite(tmp_path):
    root = str(tmp_path / "audios")
    book = os.path.join(root, "5")
    _write(os.path.join(book, "a.wav"))
    _write(os.path.join(book, "b.wav"))
    _write(os.path.join(book, "c.wav"))
    _write(os.path.join(book, "a.srt"), b"1\n00:00:00,000 --> 00:00:01,000\na\n")
    _write(os.path.join(book, "b.srt"), b"1\n00:00:00,000 --> 00:00:01,000\nb\n")

    existing = {
        "book_id": 5,
        "files": {
            "b.wav": {"status": jobs.STATUS_DONE},
            "c.wav": {"status": jobs.STATUS_RUNNING, "error": "stale"},
        },
        "created_at": 1,
    }
    state = jobs.prepare_state(base=str(tmp_path / "data"),
                               spec=_spec(root, 5, ["a.wav", "b.wav", "c.wav"]),
                               existing=existing)
    assert state["files"]["a.wav"]["status"] == jobs.STATUS_SKIPPED
    assert state["files"]["b.wav"]["status"] == jobs.STATUS_DONE
    assert state["files"]["c.wav"]["status"] == jobs.STATUS_PENDING
    assert state["files"]["c.wav"]["error"] == ""

    state = jobs.prepare_state(base=str(tmp_path / "data"),
                               spec=_spec(root, 5, ["a.wav", "b.wav", "c.wav"], overwrite=True),
                               existing=existing)
    assert state["files"]["a.wav"]["status"] == jobs.STATUS_PENDING
    assert state["files"]["b.wav"]["status"] == jobs.STATUS_PENDING


def test_merge_chunk_files(tmp_path):
    first = tmp_path / "0.srt"
    second = tmp_path / "1.srt"
    first.write_text(SRT.build_srt([SRT.Cue(0, 1, "first")]), encoding="utf-8")
    second.write_text(SRT.build_srt([SRT.Cue(10, 11, "second")]), encoding="utf-8")
    output = tmp_path / "out.srt"
    jobs._merge_chunk_files([str(first), str(second)], str(output))
    cues = SRT.parse_srt(output.read_text(encoding="utf-8"))
    assert [cue.text for cue in cues] == ["first", "second"]
    with pytest.raises(jobs.JobError):
        jobs._merge_chunk_files([str(first), str(tmp_path / "missing.srt")], str(output))


def test_chunked_resume_skips_done_chunks(monkeypatch, tmp_path):
    root = str(tmp_path / "audios")
    book = os.path.join(root, "9")
    audio = os.path.join(book, "a.wav")
    _write(audio)
    base = str(tmp_path / "data")
    chunk_dir = os.path.join(base, "tmp", "book_9", jobs._name_hash("a.wav"))
    os.makedirs(chunk_dir)
    with open(os.path.join(chunk_dir, "chunk_00000.srt"), "w", encoding="utf-8") as handle:
        handle.write(SRT.build_srt([SRT.Cue(0, 1, "chunk0")]))

    runtime = {"options": {"chunk_seconds": 1800}, "book_id": 9,
               "tmp_dir": os.path.join(base, "tmp")}
    entry = {"chunks": {"0": "done"}, "chunk_seconds": 1800}
    calls = []

    monkeypatch.setattr(jobs.scan, "ffmpeg_path", lambda: "ffmpeg")

    def fake_extract(op, runtime, audio_path, wav_path, offset, length):
        calls.append(("extract", offset))
        with open(wav_path, "wb") as handle:
            handle.write(b"wav")

    def fake_worker(op, runtime, audio_path, out_path, offset, on_processed):
        calls.append(("worker", offset))
        content = SRT.build_srt([SRT.Cue(offset, offset + 1, "chunk@%d" % offset)])
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(content)
        on_processed(0.5, 1.0)
        return {}

    monkeypatch.setattr(jobs, "_extract_chunk", fake_extract)
    monkeypatch.setattr(jobs, "run_worker", fake_worker)

    output = os.path.join(book, "a.srt")
    op = tasks.Operation("transcribe")
    jobs._process_chunked(op, runtime, "a.wav", audio, output, 3700.0, entry,
                          lambda: None, lambda value: None)
    cues = SRT.parse_srt(open(output, encoding="utf-8").read())
    assert [int(cue.start) for cue in cues] == [0, 1800, 3600]
    worker_offsets = [call[1] for call in calls if call[0] == "worker"]
    assert worker_offsets == [1800, 3600]
    assert not os.path.isdir(chunk_dir)


def test_chunk_seconds_change_resets_chunk_state(monkeypatch, tmp_path):
    root = str(tmp_path / "audios")
    book = os.path.join(root, "9")
    audio = os.path.join(book, "a.wav")
    _write(audio)
    base = str(tmp_path / "data")
    runtime = {"options": {"chunk_seconds": 1800}, "book_id": 9,
               "tmp_dir": os.path.join(base, "tmp")}
    entry = {"chunks": {"0": "done"}, "chunk_seconds": 900}
    calls = []

    monkeypatch.setattr(jobs.scan, "ffmpeg_path", lambda: "ffmpeg")

    def fake_extract(op, runtime, audio_path, wav_path, offset, length):
        with open(wav_path, "wb") as handle:
            handle.write(b"wav")

    def fake_worker(op, runtime, audio_path, out_path, offset, on_processed):
        calls.append(offset)
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(SRT.build_srt([SRT.Cue(offset, offset + 1, "x")]))
        on_processed(0.5, 1.0)
        return {}

    monkeypatch.setattr(jobs, "_extract_chunk", fake_extract)
    monkeypatch.setattr(jobs, "run_worker", fake_worker)

    output = os.path.join(book, "a.srt")
    jobs._process_chunked(tasks.Operation("transcribe"), runtime, "a.wav", audio, output,
                          3700.0, entry, lambda: None, lambda value: None)
    assert calls == [0, 1800, 3600]
    assert entry["chunk_seconds"] == 1800


def test_run_transcribe_single_file_success(monkeypatch, tmp_path):
    root = str(tmp_path / "audios")
    book = os.path.join(root, "21")
    audio = os.path.join(book, "a.wav")
    _write(audio)
    _write(os.path.join(book, "audio_collection_21.zip"))
    base = str(tmp_path / "data")
    spec = _spec(root, 21, ["a.wav"])
    spec["options"] = jobs.resolve_options({"chunk_enabled": False, "cpu_threads": 1})
    updates = []
    finished = []

    def fake_worker(op, runtime, audio_path, out_path, offset, on_processed):
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(SRT.build_srt([SRT.Cue(0, 1, "hi")]))
        on_processed(0.6, 1.0)
        return {}

    monkeypatch.setattr(jobs, "run_worker", fake_worker)
    monkeypatch.setattr(jobs.scan, "refresh_duration", lambda path: None)

    runtime = {
        "data_root": base,
        "python_exe": sys.executable,
        "vendor": str(tmp_path / "vendor"),
        "worker_path": "worker.py",
        "model_dir": str(tmp_path / "model"),
        "cwd": base,
        "host_update": lambda percent, message: updates.append((percent, message)),
        "host_finish": lambda error: finished.append(error),
    }
    op = tasks.Operation("transcribe")
    jobs.run_transcribe(op, spec, runtime)

    state = jobs.load_job(base, 21)
    assert state["status"] == "completed"
    assert state["files"]["a.wav"]["status"] == "done"
    assert os.path.isfile(os.path.join(book, "a.srt"))
    assert updates
    assert finished == [None]
    assert not os.path.exists(os.path.join(book, "audio_collection_21.zip"))


def test_run_transcribe_failure_marks_job_failed(monkeypatch, tmp_path):
    root = str(tmp_path / "audios")
    book = os.path.join(root, "22")
    _write(os.path.join(book, "a.wav"))
    base = str(tmp_path / "data")
    spec = _spec(root, 22, ["a.wav"])
    spec["options"] = jobs.resolve_options({"chunk_enabled": False})
    finished = []

    def fake_worker(op, runtime, audio_path, out_path, offset, on_processed):
        raise jobs.JobError("boom")

    monkeypatch.setattr(jobs, "run_worker", fake_worker)
    monkeypatch.setattr(jobs.scan, "refresh_duration", lambda path: None)

    runtime = {
        "data_root": base,
        "python_exe": sys.executable,
        "vendor": str(tmp_path / "vendor"),
        "worker_path": "worker.py",
        "model_dir": str(tmp_path / "model"),
        "cwd": base,
        "host_update": lambda percent, message: None,
        "host_finish": lambda error: finished.append(error),
    }
    op = tasks.Operation("transcribe")
    jobs.run_transcribe(op, spec, runtime)

    state = jobs.load_job(base, 22)
    assert state["status"] == "failed"
    assert state["files"]["a.wav"]["status"] == "failed"
    assert op.status == tasks.STATUS_FAILED
    assert finished and finished[0]
