# -*- coding: utf-8 -*-
import pytest

from backend import srt


def test_format_timestamp():
    assert srt.format_timestamp(0) == "00:00:00,000"
    assert srt.format_timestamp(3661.234) == "01:01:01,234"
    assert srt.format_timestamp(-3) == "00:00:00,000"
    assert srt.format_timestamp(59.9996) == "00:01:00,000"


def test_normalize_text():
    assert srt.normalize_text("  a \n\n b  ") == "a b"
    assert srt.normalize_text(None) == ""


def test_split_text_respects_limit_and_keeps_content():
    text = ("第一句话内容比较长。" * 8) + ("第二句话也有一点长。" * 8)
    chunks = srt.split_text(text, 20)
    assert len(chunks) > 1
    assert all(len(chunk) <= 20 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_split_cue_timing_is_monotonic_and_preserves_bounds():
    text = "这是一个非常长的句子，" * 6
    cue = srt.Cue(10.0, 22.0, text)
    pieces = srt.split_cue(cue, max_chars=12, max_duration=4.0)
    assert len(pieces) > 1
    assert pieces[0].start == pytest.approx(10.0)
    assert pieces[-1].end == pytest.approx(22.0)
    for left, right in zip(pieces, pieces[1:]):
        assert right.start == pytest.approx(left.end)
    for piece in pieces:
        assert piece.end > piece.start
        assert piece.text


def test_short_cue_is_unchanged():
    cue = srt.Cue(1.0, 2.0, "短句")
    assert srt.split_cue(cue) == [cue]


def test_build_and_parse_roundtrip():
    cues = [srt.Cue(0.0, 1.5, "你好"), srt.Cue(1.6, 3.25, "世界")]
    content = srt.build_srt(cues)
    parsed = srt.parse_srt(content)
    assert len(parsed) == 2
    assert parsed[0].text == "你好"
    assert parsed[0].start == pytest.approx(0.0)
    assert parsed[1].end == pytest.approx(3.25)


def test_dedupe_consecutive_merges_same_text():
    cues = [
        srt.Cue(0.0, 1.0, "重复"),
        srt.Cue(1.2, 2.0, "重复"),
        srt.Cue(3.0, 4.0, "不同"),
    ]
    merged = srt.dedupe_consecutive(cues)
    assert len(merged) == 2
    assert merged[0].start == 0.0
    assert merged[0].end == 2.0


def test_merge_srt_sorts_and_offsets():
    first = srt.build_srt([srt.Cue(0.0, 1.0, "a")])
    second = srt.build_srt([srt.Cue(10.0, 11.0, "b")])
    merged = srt.parse_srt(srt.merge_srt([second, first]))
    assert [cue.text for cue in merged] == ["a", "b"]


def test_looks_hallucinated():
    class Segment(object):
        no_speech_prob = 0.9
        avg_logprob = -2.0

    class Good(object):
        no_speech_prob = 0.1
        avg_logprob = -0.2

    assert srt.looks_hallucinated(Segment())
    assert not srt.looks_hallucinated(Good())
