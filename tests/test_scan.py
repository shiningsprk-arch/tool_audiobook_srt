# -*- coding: utf-8 -*-
import os

import pytest

from backend import scan


def _write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def test_scan_books_and_subtitle_coverage(tmp_path):
    root = tmp_path / "audios"
    book = root / "12"
    _write(str(book / "0001_第一章.wav"))
    _write(str(book / "0002_第二章.mp3"))
    _write(str(book / "0001_第一章.srt"), b"1\n00:00:00,000 --> 00:00:01,000\na\n")
    _write(str(book / "0002_第二章.vtt"), b"WEBVTT\n")
    _write(str(book / "0002_第二章.srt"), b"1\n00:00:00,000 --> 00:00:01,000\nb\n")
    _write(str(book / "cover.jpg"))
    _write(str(book / "audio_collection_12.zip"))
    _write(str(book / "x_图书在版编目CIP数据.wav"))

    other = root / "13"
    _write(str(other / "0001.mp3"))
    _write(str(root / "not-a-book" / "0001.mp3"))

    books = scan.scan_books(str(root), {12: {"title": "Book 12", "authors": ["A"]}})
    assert [item["book_id"] for item in books] == [13, 12]
    target = books[1]
    assert target["title"] == "Book 12"
    assert target["audio_count"] == 2
    assert target["covered_count"] == 2
    assert target["missing_count"] == 0
    names = [item["filename"] for item in target["files"]]
    assert names == ["0001_第一章.wav", "0002_第二章.mp3"]
    assert target["files"][0]["subtitle"] == "0001_第一章.srt"
    assert target["files"][1]["subtitle"] == "0002_第二章.srt"


def test_select_files_modes(tmp_path):
    root = tmp_path / "audios"
    _write(str(root / "1" / "a.wav"))
    _write(str(root / "1" / "b.wav"))
    _write(str(root / "1" / "a.srt"))
    book = scan.book_entry(str(root), 1)
    assert scan.select_files(book["files"], "missing") == ["b.wav"]
    assert scan.select_files(book["files"], "all") == ["a.wav", "b.wav"]
    assert scan.select_files(book["files"], "selected", ["b.wav"]) == ["b.wav"]
    assert scan.select_files(book["files"], "selected", ["nope.wav"]) == []
    with pytest.raises(ValueError):
        scan.select_files(book["files"], "bogus")


def test_path_safety(tmp_path):
    root = tmp_path / "audios"
    _write(str(root / "1" / "a.wav"))
    with pytest.raises(ValueError):
        scan.safe_book_dir(str(root), "../1")
    with pytest.raises(ValueError):
        scan.audio_path(str(root), 1, "../a.wav")
    assert scan.audio_path(str(root), 1, "a.wav").endswith("a.wav")
    assert scan.srt_path(str(root), 1, "a.wav").endswith("a.srt")


def test_skip_marker_matches_host_semantics(tmp_path):
    root = tmp_path / "audios"
    _write(str(root / "2" / "图书在版编目CIP数据_开头.wav"))
    _write(str(root / "2" / "前言_图书在版编目CIP数据.wav"))
    book = scan.book_entry(str(root), 2)
    assert [item["filename"] for item in book["files"]] == ["图书在版编目CIP数据_开头.wav"]


def test_delete_cached_audio_zip_only_removes_cache(tmp_path):
    root = tmp_path / "audios"
    _write(str(root / "7" / "a.wav"))
    _write(str(root / "7" / "audio_collection_7.zip"))
    _write(str(root / "7" / "keep.zip"))
    assert scan.delete_cached_audio_zip(str(root), 7) is True
    assert not os.path.exists(str(root / "7" / "audio_collection_7.zip"))
    assert os.path.exists(str(root / "7" / "keep.zip"))
    assert scan.delete_cached_audio_zip(str(root), 7) is False


def test_missing_metadata_title_is_empty(tmp_path):
    root = tmp_path / "audios"
    _write(str(root / "31" / "a.wav"))
    book = scan.book_entry(str(root), 31)
    assert book["title"] == ""
