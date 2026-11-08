# -*- coding: utf-8 -*-
"""SRT 构建、长句切分、解析与合并。

纯标准库、无宿主依赖，便于离线单测；worker 与任务层共用同一套时间轴规则。
"""
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional

_SPACE_RE = re.compile(r"[ \t\u3000]+")
_TIME_LINE_RE = re.compile(
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
)
_SPLIT_CHARS = "。！？；，、,.!?;"
_SENTENCE_CHARS = "。！？!?"


@dataclass
class Cue:
    start: float
    end: float
    text: str


def normalize_text(text: str) -> str:
    """折叠空白、去首尾、丢掉空行；不做任何字符替换。"""
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return " ".join(line for line in lines if line)


def format_timestamp(seconds: float) -> str:
    ms = int(round(max(0.0, float(seconds)) * 1000))
    hours, rem = divmod(ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, millis = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def _parse_timestamp(hours, minutes, secs, millis) -> float:
    return (
        int(hours or 0) * 3600
        + int(minutes) * 60
        + int(secs)
        + int(str(millis).ljust(3, "0")[:3]) / 1000.0
    )


def _hard_split(text: str, max_chars: int) -> List[str]:
    """无标点兜底：优先在最后一个空格处断开，否则硬切。"""
    pieces = []
    rest = text
    while len(rest) > max_chars:
        cut = rest.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return [piece for piece in pieces if piece]


def split_text(text: str, max_chars: int = 48) -> List[str]:
    """按标点切成不超长的小块，再做贪心合并。

    目标不是做字幕排版，只是避免单条字幕一次盖住半个屏幕；切不动就保留原样。
    """
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    rough = []
    buf = ""
    for char in text:
        buf += char
        if char in _SPLIT_CHARS and len(buf) >= max(8, max_chars // 3):
            rough.append(buf.strip())
            buf = ""
    if buf.strip():
        rough.append(buf.strip())

    merged = []
    for piece in rough:
        if len(piece) > max_chars:
            if merged:
                merged.append(None)  # 强制先落盘，保持顺序
            merged.extend(_hard_split(piece, max_chars))
            continue
        if merged and merged[-1] is not None and len(merged[-1]) + len(piece) <= max_chars:
            merged[-1] += piece
        else:
            merged.append(piece)
    return [piece for piece in merged if piece]


def split_cue(cue: Cue, max_chars: int = 48, max_duration: float = 8.0) -> List[Cue]:
    """把一条长 cue 切成多条，时间按字符数比例分配。"""
    text = normalize_text(cue.text)
    if not text:
        return []
    duration = max(0.0, cue.end - cue.start)
    if len(text) <= max_chars and duration <= max_duration:
        return [Cue(cue.start, cue.end, text)]
    if duration < 1.0 and len(text) <= max_chars * 1.5:
        return [Cue(cue.start, cue.end, text)]

    chunks = split_text(text, max_chars)
    if len(chunks) <= 1:
        return [Cue(cue.start, cue.end, text)]

    weights = [max(1, len(chunk)) for chunk in chunks]
    total_weight = float(sum(weights))
    cues = []
    cursor = cue.start
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            end = cue.end
        else:
            end = cursor + duration * weights[index] / total_weight
        cues.append(Cue(cursor, end, chunk))
        cursor = end
    return cues


def dedupe_consecutive(cues: Iterable[Cue], gap: float = 1.0) -> List[Cue]:
    """合并连续重复文本（Whisper 在静音/音乐处常见的复读）。"""
    result = []
    for cue in cues:
        text = normalize_text(cue.text)
        if not text or cue.end <= cue.start:
            continue
        prev = result[-1] if result else None
        if prev and text == normalize_text(prev.text) and cue.start <= prev.end + gap:
            result[-1] = Cue(prev.start, max(prev.end, cue.end), prev.text)
        else:
            result.append(Cue(cue.start, cue.end, text))
    return result


def build_srt(cues: Iterable[Cue]) -> str:
    blocks = []
    index = 1
    for cue in dedupe_consecutive(cues):
        blocks.append(
            "%d\n%s --> %s\n%s\n"
            % (
                index,
                format_timestamp(cue.start),
                format_timestamp(cue.end),
                normalize_text(cue.text),
            )
        )
        index += 1
    return "\n".join(blocks)


def parse_srt(content: str) -> List[Cue]:
    cues = []
    if not content:
        return cues
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    for block in re.split(r"\n\s*\n", normalized):
        lines = block.split("\n")
        time_index = None
        match = None
        for index, line in enumerate(lines):
            found = _TIME_LINE_RE.search(line)
            if found:
                time_index = index
                match = found
                break
        if time_index is None:
            continue
        start = _parse_timestamp(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _parse_timestamp(match.group(5), match.group(6), match.group(7), match.group(8))
        text = normalize_text(" ".join(lines[time_index + 1:]))
        if text and end > start:
            cues.append(Cue(start, end, text))
    cues.sort(key=lambda cue: (cue.start, cue.end))
    return cues


def merge_srt(contents: Iterable[str]) -> str:
    cues = []
    for content in contents:
        cues.extend(parse_srt(content))
    cues.sort(key=lambda cue: (cue.start, cue.end))
    return build_srt(cues)


def offset_cues(cues: Iterable[Cue], offset: float) -> List[Cue]:
    return [Cue(cue.start + offset, cue.end + offset, cue.text) for cue in cues]


def looks_hallucinated(segment) -> bool:
    """faster-whisper 段级的保守幻觉过滤：静音高概率且置信度低才丢。"""
    no_speech = getattr(segment, "no_speech_prob", 0.0) or 0.0
    logprob = getattr(segment, "avg_logprob", 0.0) or 0.0
    try:
        no_speech = float(no_speech)
    except (TypeError, ValueError):
        no_speech = 0.0
    try:
        logprob = float(logprob)
    except (TypeError, ValueError):
        logprob = 0.0
    return no_speech > 0.6 and logprob < -1.0


def cue_count(content: str) -> int:
    return len(parse_srt(content))


def is_sentence_end(char: str) -> bool:
    return char in _SENTENCE_CHARS


def clamp_cue(cue: Cue, duration: Optional[float] = None) -> Cue:
    end = max(cue.start, cue.end)
    if duration is not None:
        end = min(end, max(cue.start, duration))
    return Cue(max(0.0, cue.start), end, cue.text)
