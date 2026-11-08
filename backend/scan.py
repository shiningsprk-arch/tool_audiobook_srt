# -*- coding: utf-8 -*-
"""扫描有声书目录：书单、逐文件字幕覆盖、时长探测、可处理文件选择。

只读宿主音频目录，不依赖 webserver 模块，便于离线单测。
"""
import functools
import logging
import os
import re
import shutil
import subprocess

AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".wav", ".wma", ".opus")
SUBTITLE_EXTS = (".srt", ".vtt")
# 与宿主 webserver/handlers/audio.py 的 SKIP_AUDIO_FILE_PREFIX 保持一致：
# 播放器不列的音频，字幕工具也不处理。
SKIP_AUDIO_MARKER = "图书在版编目CIP数据"
CACHE_ZIP_PREFIX = "audio_collection_"

_NUM_RE = re.compile(r"(\d+)")


def natural_key(name):
    return [int(part) if part.isdigit() else part.lower() for part in _NUM_RE.split(name)]


def is_audio_file(name):
    return name.lower().endswith(AUDIO_EXTS)


def is_subtitle_file(name):
    return name.lower().endswith(SUBTITLE_EXTS)


def list_audio_files(audio_dir):
    """返回可处理音频文件名（已排序、已过滤宿主会跳过的 Cip 数据文件）。"""
    try:
        names = os.listdir(audio_dir)
    except OSError as err:
        logging.warning("[audiobook_srt] 无法读取音频目录 %s: %s", audio_dir, err)
        return []
    files = []
    for name in names:
        if not is_audio_file(name):
            continue
        if CACHE_ZIP_PREFIX and name.startswith(CACHE_ZIP_PREFIX):
            continue
        # 与宿主 audio.py 的 `file.find(marker) > 0` 完全一致：文件名以该前缀开头时
        # 宿主仍会列出，工具也照常处理。
        if SKIP_AUDIO_MARKER and name.find(SKIP_AUDIO_MARKER) > 0:
            continue
        if not os.path.isfile(os.path.join(audio_dir, name)):
            continue
        files.append(name)
    files.sort(key=natural_key)
    return files


def subtitle_map(audio_dir):
    """stem -> 字幕文件名；同 stem 有 .srt 时优先 .srt（与宿主排序口径基本一致）。"""
    mapping = {}
    try:
        names = sorted(os.listdir(audio_dir))
    except OSError:
        return mapping
    for name in names:
        if not is_subtitle_file(name):
            continue
        stem, ext = os.path.splitext(name)
        if stem not in mapping or ext.lower() == ".srt":
            mapping[stem] = name
    return mapping


def _book_dir(root, book_id):
    if not str(book_id).isdigit():
        raise ValueError("book_id 必须是数字")
    root_real = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root_real, str(int(book_id))))
    if os.path.commonpath([root_real, path]) != root_real:
        raise ValueError("书籍目录越界")
    return path


def safe_book_dir(root, book_id):
    return _book_dir(root, book_id)


def _safe_file_path(audio_dir, filename):
    if not filename or filename != os.path.basename(filename):
        raise ValueError("非法文件名")
    if filename in (".", "..") or "/" in filename or "\\" in filename:
        raise ValueError("非法文件名")
    path = os.path.realpath(os.path.join(audio_dir, filename))
    if os.path.commonpath([os.path.realpath(audio_dir), path]) != os.path.realpath(audio_dir):
        raise ValueError("音频路径越界")
    return path


def audio_path(root, book_id, filename):
    audio_dir = _book_dir(root, book_id)
    path = _safe_file_path(audio_dir, filename)
    if not os.path.isfile(path):
        raise ValueError("音频文件不存在")
    return path


def srt_path(root, book_id, filename):
    audio_dir = _book_dir(root, book_id)
    path = _safe_file_path(audio_dir, filename)
    return os.path.splitext(path)[0] + ".srt"


def book_entry(root, book_id, title="", authors=None, job_state=None):
    audio_dir = _book_dir(root, book_id)
    files = list_audio_files(audio_dir)
    subs = subtitle_map(audio_dir)
    items = []
    for name in files:
        stem = os.path.splitext(name)[0]
        subtitle = subs.get(stem)
        size = 0
        try:
            size = os.path.getsize(os.path.join(audio_dir, name))
        except OSError:
            pass
        items.append({
            "filename": name,
            "size": size,
            "subtitle": subtitle or "",
            "covered": bool(subtitle),
        })
    covered = sum(1 for item in items if item["covered"])
    pending = sum(1 for item in items if not item["covered"])
    authored = authors or []
    if isinstance(authored, str):
        authored = [authored]
    return {
        "book_id": int(book_id),
        "title": title or "",
        "authors": [str(author) for author in authored if author],
        "audio_dir": audio_dir,
        "audio_count": len(items),
        "covered_count": covered,
        "missing_count": pending,
        "total_size": sum(item["size"] for item in items),
        "files": items,
        "job": job_state or None,
    }


def scan_books(root, metadata=None):
    """扫描音频根目录下所有数字书籍目录。

    metadata: {book_id: {"title": str, "authors": [...]}}，由调用方一次批量查书库后传入。
    """
    if not os.path.isdir(root):
        return []
    metadata = metadata or {}
    books = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: natural_key(entry.name))
    except OSError as err:
        logging.warning("[audiobook_srt] 无法读取音频根目录 %s: %s", root, err)
        return []
    for entry in entries:
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        book_id = int(entry.name)
        meta = metadata.get(book_id) or {}
        try:
            info = book_entry(root, book_id, meta.get("title", ""), meta.get("authors"))
        except (OSError, ValueError) as err:
            logging.warning("[audiobook_srt] 跳过书籍 %s: %s", book_id, err)
            continue
        if info["audio_count"] == 0:
            continue
        books.append(info)
    books.sort(key=lambda item: (
        item["missing_count"] == 0,
        -item["missing_count"],
        (item["title"] or "").lower(),
    ))
    return books


def select_files(files, mode="missing", selected=None):
    """按 UI 模式挑出本次要处理的文件（files 是 book_entry 的 files）。"""
    mode = (mode or "missing").lower()
    if mode == "all":
        return [item["filename"] for item in files]
    if mode == "missing":
        return [item["filename"] for item in files if not item["covered"]]
    if mode == "selected":
        wanted = set(selected or [])
        available = {item["filename"] for item in files}
        return [item["filename"] for item in files if item["filename"] in wanted and item["filename"] in available]
    raise ValueError("未知的文件选择模式: %s" % mode)


_duration_cache = {}


@functools.lru_cache(maxsize=1)
def ffprobe_path():
    return shutil.which("ffprobe")


@functools.lru_cache(maxsize=1)
def ffmpeg_path():
    return shutil.which("ffmpeg")


def probe_duration(path):
    """ffprobe 读取音频时长（秒）；不可用时返回 None。结果按 mtime+size 缓存。"""
    probe = ffprobe_path()
    if not probe:
        return None
    try:
        stat = os.stat(path)
        key = (os.path.realpath(path), stat.st_mtime, stat.st_size)
    except OSError:
        return None
    if key in _duration_cache:
        return _duration_cache[key]
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=20,
        )
        value = float(result.stdout.decode("utf-8", "replace").strip())
        if value <= 0:
            value = None
    except (OSError, ValueError, subprocess.SubprocessError):
        value = None
    _duration_cache[key] = value
    return value


def duration_cached(path):
    return probe_duration(path)


def refresh_duration(path):
    try:
        stat = os.stat(path)
        _duration_cache.pop((os.path.realpath(path), stat.st_mtime, stat.st_size), None)
    except OSError:
        pass
    return probe_duration(path)


def delete_cached_audio_zip(root, book_id):
    """补完字幕后删掉宿主缓存的整本音频 zip，否则下次下载仍不含新字幕。"""
    try:
        audio_dir = _book_dir(root, book_id)
    except ValueError:
        return False
    target = os.path.join(audio_dir, "%s%d.zip" % (CACHE_ZIP_PREFIX, int(book_id)))
    if not os.path.isfile(target):
        return False
    try:
        os.remove(target)
        logging.info("[audiobook_srt] 已清理音频 zip 缓存: %s", target)
        return True
    except OSError as err:
        logging.warning("[audiobook_srt] 清理音频 zip 缓存失败 %s: %s", target, err)
        return False
