# -*- coding: utf-8 -*-
"""不装 faster-whisper 也验证 worker 子进程：用一个假模块走真实命令行与 JSON 协议。"""
import json
import os
import subprocess
import sys

from backend import srt as SRT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, content=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def test_worker_protocol_with_fake_faster_whisper(tmp_path):
    vendor = str(tmp_path / "vendor")
    _write(os.path.join(vendor, "faster_whisper", "__init__.py"), """
class _Info(object):
    duration = 3.0
    language = "zh"
    language_probability = 0.99

class _Segment(object):
    def __init__(self, start, end, text, no_speech_prob=0.1, avg_logprob=-0.2):
        self.start = start
        self.end = end
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob

class WhisperModel(object):
    def __init__(self, model, device="cpu", compute_type="int8", cpu_threads=2):
        assert device == "cpu"
        assert compute_type == "int8"
        assert cpu_threads >= 1

    def transcribe(self, audio, **kwargs):
        assert kwargs.get("language") == "zh"
        assert kwargs.get("vad_filter") is True
        assert kwargs.get("condition_on_previous_text") is False

        def segments():
            yield _Segment(0.0, 1.2, "第一句。")
            yield _Segment(1.2, 2.5, "第二句。")
            yield _Segment(2.5, 3.0, "幻觉句。", no_speech_prob=0.95, avg_logprob=-2.0)

        return segments(), _Info()
""".encode("utf-8"))
    model = str(tmp_path / "model")
    _write(os.path.join(model, "config.json"), b"{}")
    _write(os.path.join(model, "model.bin"), b"x")
    _write(os.path.join(model, "tokenizer.json"), b"{}")
    _write(os.path.join(model, "vocabulary.txt"), b"x")
    audio = str(tmp_path / "a.wav")
    _write(audio, b"fake")
    out = str(tmp_path / "a.srt")

    cmd = [
        sys.executable, "-u", os.path.join(ROOT, "backend", "worker.py"),
        "--audio", audio, "--out", out, "--model", model,
        "--vendor", vendor, "--language", "zh", "--compute-type", "int8",
        "--cpu-threads", "2", "--vad", "1",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    lines = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines() if line.strip()]
    kinds = [line["type"] for line in lines]
    assert "info" in kinds
    assert "done" in kinds
    assert kinds[-1] == "done"
    assert os.path.isfile(out)
    cues = SRT.parse_srt(open(out, encoding="utf-8").read())
    assert [cue.text for cue in cues] == ["第一句。", "第二句。"]
