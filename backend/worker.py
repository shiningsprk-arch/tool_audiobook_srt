# -*- coding: utf-8 -*-
"""独立转写进程：唯一 import faster_whisper 的地方。

由 jobs.py 用 `python -u worker.py ...` 起在独立进程组里；stdout 只输出 JSON Lines，
父进程逐行解析进度。依赖目录由 `--vendor` / PYTHONPATH 注入，不污染 MyBooks 主进程。
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import traceback


def _load_local_srt():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "srt.py")
    spec = importlib.util.spec_from_file_location("audiobook_srt_local_srt", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SRT = _load_local_srt()


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_parser():
    parser = argparse.ArgumentParser(description="audiobook_srt worker")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--vendor", default="")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--vad", type=int, default=1)
    parser.add_argument("--initial-prompt", default="")
    parser.add_argument("--max-chars", type=int, default=48)
    parser.add_argument("--max-duration", type=float, default=8.0)
    parser.add_argument("--offset", type=float, default=0.0)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.vendor:
        sys.path.insert(0, args.vendor)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    try:
        from faster_whisper import WhisperModel
    except Exception as err:  # noqa: BLE001
        emit({"type": "error", "code": "deps.import",
              "message": "无法导入 faster_whisper：%s" % err})
        return 2

    try:
        model = WhisperModel(
            args.model,
            device="cpu",
            compute_type=args.compute_type,
            cpu_threads=max(1, int(args.cpu_threads)),
        )
        language = None if args.language in ("", "auto") else args.language
        transcribe_args = dict(
            language=language,
            task="transcribe",
            beam_size=max(1, int(args.beam_size)),
            vad_filter=bool(args.vad),
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            word_timestamps=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
        )
        if args.initial_prompt:
            transcribe_args["initial_prompt"] = args.initial_prompt
        segments, info = model.transcribe(args.audio, **transcribe_args)

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        emit({
            "type": "info",
            "duration": duration,
            "language": getattr(info, "language", "") or "",
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        })

        cues = []
        last_emit = 0.0
        for segment in segments:
            if SRT.looks_hallucinated(segment):
                continue
            text = SRT.normalize_text(getattr(segment, "text", ""))
            if not text:
                continue
            start = float(getattr(segment, "start", 0.0) or 0.0)
            end = float(getattr(segment, "end", 0.0) or 0.0)
            if end <= start:
                continue
            cue = SRT.Cue(start + args.offset, end + args.offset, text)
            cues.extend(SRT.split_cue(cue, args.max_chars, args.max_duration))

            now = time.time()
            if now - last_emit >= 0.5:
                last_emit = now
                emit({
                    "type": "progress",
                    "processed": end,
                    "duration": duration,
                    "offset": args.offset,
                    "percent": (min(100.0, end * 100.0 / duration) if duration else 0.0),
                })

        content = SRT.build_srt(cues)
        if not content.strip():
            emit({"type": "error", "code": "empty",
                  "message": "没有识别出可用字幕（可能是纯音乐/静音或语言设置不符）"})
            return 3

        part = args.out + ".part"
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(part, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(part, args.out)
        emit({"type": "done", "cues": len(SRT.parse_srt(content)),
              "duration": duration, "offset": args.offset})
        return 0
    except Exception as err:  # noqa: BLE001
        emit({"type": "error", "code": "worker.error",
              "message": "%s" % err, "traceback": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    sys.exit(main())
