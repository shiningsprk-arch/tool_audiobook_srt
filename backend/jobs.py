# -*- coding: utf-8 -*-
"""转写任务：逐文件状态、长文件分片、SRT 合并、断点续跑、取消。

不 import 宿主模块；宿主的后台任务进度通过 runtime 里的回调透出，
便于离线单测与复用。
"""
import collections
import json
import logging
import math
import os
import shutil
import threading
import time

from . import scan
from . import srt as SRT
from . import tasks

JOB_VERSION = 1
DEFAULT_OPTIONS = {
    "model": "small",
    "language": "zh",
    "zh_script": "t2s",
    "compute_type": "int8",
    "cpu_threads": min(4, max(1, (os.cpu_count() or 2) - 1)),
    "beam_size": 5,
    "vad": True,
    "initial_prompt": "",
    "overwrite": False,
    "chunk_enabled": True,
    "chunk_seconds": 1800,
    "max_chars": 48,
    "max_duration": 8.0,
}
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
_FINISHED = (STATUS_DONE, STATUS_SKIPPED, STATUS_FAILED)


class JobError(RuntimeError):
    pass


class ChunkUnavailable(RuntimeError):
    pass


def job_file(base, book_id):
    return os.path.join(os.path.join(base, "jobs"), "%d.json" % int(book_id))


def _atomic_write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)


def save_job(base, state):
    state["updated_at"] = time.time()
    _atomic_write(job_file(base, state["book_id"]),
                  json.dumps(state, ensure_ascii=False, indent=2))


def load_job(base, book_id):
    path = job_file(base, book_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or "files" not in state:
        return None
    return state


def normalize_job(state, active=False):
    if not state:
        return None
    data = json.loads(json.dumps(state, ensure_ascii=False))
    if data.get("status") == STATUS_RUNNING and not active:
        data["status"] = "interrupted"
    data.pop("options", None)
    return data


def latest_job(base, active_book_id=None):
    jobs_dir = os.path.join(base, "jobs")
    if not os.path.isdir(jobs_dir):
        return None
    latest = None
    latest_mtime = 0.0
    for name in os.listdir(jobs_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(jobs_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest = path
    if not latest:
        return None
    try:
        with open(latest, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    active = bool(active_book_id and int(state.get("book_id", -1)) == int(active_book_id))
    return normalize_job(state, active=active)


def _entry(state, filename):
    entry = state["files"].get(filename)
    if not isinstance(entry, dict):
        entry = {}
        state["files"][filename] = entry
    return entry


def prepare_state(base, spec, existing):
    now = time.time()
    state = existing if existing else {}
    state.setdefault("version", JOB_VERSION)
    state["book_id"] = int(spec["book_id"])
    state["title"] = spec.get("title") or ""
    state["status"] = STATUS_RUNNING
    state["error"] = ""
    state["current"] = ""
    state["created_at"] = state.get("created_at") or now
    state["selected"] = list(spec["selected"])
    state["options"] = dict(spec["options"])
    state.setdefault("files", {})
    valid_names = {item["filename"] for item in spec["files"]}
    state["files"] = {name: entry for name, entry in state["files"].items()
                      if name in valid_names and isinstance(entry, dict)}

    subs = scan.subtitle_map(spec["audio_dir"])
    for entry in state["files"].values():
        if isinstance(entry, dict) and entry.get("status") == STATUS_RUNNING:
            entry["status"] = STATUS_PENDING
            entry["error"] = ""
    for item in spec["files"]:
        filename = item["filename"]
        entry = _entry(state, filename)
        entry.setdefault("status", STATUS_PENDING)
        entry.setdefault("error", "")
        entry.setdefault("chunks", {})
        entry["size"] = item["size"]
        if filename not in state["selected"]:
            continue
        output = scan.srt_path(spec["audio_root"], spec["book_id"], filename)
        overwrite = bool(spec["options"].get("overwrite"))
        if not overwrite and entry.get("status") == STATUS_DONE and os.path.isfile(output):
            continue
        if not overwrite and subs.get(os.path.splitext(filename)[0]):
            entry["status"] = STATUS_SKIPPED
            entry["error"] = ""
            continue
        entry["status"] = STATUS_PENDING
        entry["error"] = ""
    return state


def _nice_argv(argv):
    """NAS 上转写和 Web 服务抢 CPU：能用 nice 就降一级优先级。"""
    if os.name == "posix" and shutil.which("nice"):
        return ["nice", "-n", "10"] + list(argv)
    return list(argv)


def _worker_env(vendor, options):
    env = dict(os.environ)
    env["PYTHONPATH"] = vendor + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env["HF_HUB_DISABLE_XET"] = "1"
    threads = str(max(1, int(options.get("cpu_threads") or 2)))
    env["OMP_NUM_THREADS"] = threads
    env["MKL_NUM_THREADS"] = threads
    return env


def _stderr_tail(lines):
    return "\n".join(list(lines)[-12:]).strip()


def run_worker(op, runtime, audio_path, out_path, offset, on_processed):
    """起 worker 子进程，解析 JSON Lines；成功返回 info。"""
    options = runtime["options"]
    argv = [
        runtime["python_exe"], "-u", runtime["worker_path"],
        "--audio", audio_path,
        "--out", out_path,
        "--model", runtime["model_dir"],
        "--vendor", runtime["vendor"],
        "--language", str(options.get("language") or "zh"),
        "--zh-script", str(options.get("zh_script") or "t2s"),
        "--compute-type", str(options.get("compute_type") or "int8"),
        "--cpu-threads", str(max(1, int(options.get("cpu_threads") or 2))),
        "--beam-size", str(max(1, int(options.get("beam_size") or 5))),
        "--vad", "1" if options.get("vad", True) else "0",
        "--initial-prompt", str(options.get("initial_prompt") or ""),
        "--max-chars", str(max(12, int(options.get("max_chars") or 48))),
        "--max-duration", str(max(2.0, float(options.get("max_duration") or 8.0))),
        "--offset", "%.3f" % float(offset or 0.0),
    ]
    op.log("worker: %s" % os.path.basename(audio_path))
    proc = tasks.popen_group(
        _nice_argv(argv),
        stdin=subprocess_devnull(),
        stdout=subprocess_pipe(),
        stderr=subprocess_pipe(),
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        cwd=runtime.get("cwd") or None,
        env=_worker_env(runtime["vendor"], options),
    )
    op.attach_process(proc)
    stderr_lines = collections.deque(maxlen=40)
    reader = threading.Thread(
        target=_drain_stderr, args=(proc.stderr, stderr_lines), daemon=True)
    reader.start()

    info = {}
    worker_error = ""
    done_seen = False
    try:
        for line in proc.stdout:
            text = (line or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                stderr_lines.append(text)
                continue
            kind = payload.get("type")
            if kind == "info":
                info = payload
            elif kind == "progress":
                on_processed(float(payload.get("processed") or 0.0),
                             float(payload.get("duration") or 0.0))
            elif kind == "log":
                op.log(payload.get("message") or "")
            elif kind == "error":
                worker_error = payload.get("message") or "worker 失败"
                if payload.get("traceback"):
                    stderr_lines.append(payload["traceback"])
            elif kind == "done":
                done_seen = True
                info.update(payload)
            if op.is_cancelled():
                break
        returncode = proc.wait()
    finally:
        op.attach_process(None)
        reader.join(timeout=2)

    if op.is_cancelled():
        raise tasks.Cancelled("已取消")
    if worker_error:
        raise JobError(worker_error)
    if returncode != 0:
        raise JobError("worker 退出码 %s：%s" % (returncode, _stderr_tail(stderr_lines)))
    if not done_seen or not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        raise JobError("worker 没有产出字幕：%s" % _stderr_tail(stderr_lines))
    return info


def _drain_stderr(stream, lines):
    try:
        for line in stream:
            line = line.strip()
            if line:
                lines.append(line)
    except (OSError, ValueError):
        pass


def subprocess_pipe():
    import subprocess
    return subprocess.PIPE


def subprocess_devnull():
    import subprocess
    return subprocess.DEVNULL


def _extract_chunk(op, runtime, audio_path, wav_path, offset, length):
    import subprocess
    ffmpeg = scan.ffmpeg_path()
    if not ffmpeg:
        raise ChunkUnavailable("ffmpeg 不可用")
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", "%.3f" % offset, "-i", audio_path,
        "-t", "%.3f" % length, "-vn", "-ac", "1", "-ar", "16000",
        wav_path,
    ]
    proc = tasks.popen_group(
        _nice_argv(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        env=_worker_env(runtime["vendor"], runtime["options"]),
    )
    op.attach_process(proc)
    try:
        _out, err = proc.communicate()
    finally:
        op.attach_process(None)
    if op.is_cancelled():
        raise tasks.Cancelled("已取消")
    if proc.returncode != 0 or not os.path.isfile(wav_path):
        raise ChunkUnavailable((err or "").strip()[-400:] or "ffmpeg 切片失败")


def _merge_chunk_files(chunk_files, output_path):
    contents = []
    for path in chunk_files:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            raise JobError("分片字幕缺失：%s" % os.path.basename(path))
        with open(path, "r", encoding="utf-8") as handle:
            contents.append(handle.read())
    content = SRT.merge_srt(contents)
    if not content.strip():
        raise JobError("分片合并后没有字幕")
    part = output_path + ".part"
    with open(part, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(part, output_path)


def _process_chunked(op, runtime, filename, audio_path, output_path, duration,
                     entry, save_state, set_file_progress):
    chunk_seconds = max(60, int(runtime["options"].get("chunk_seconds") or 1800))
    chunk_dir = os.path.join(
        runtime["tmp_dir"], "book_%d" % int(runtime["book_id"]),
        _name_hash(filename))
    os.makedirs(chunk_dir, exist_ok=True)
    if entry.get("chunk_seconds") != chunk_seconds:
        entry["chunks"] = {}
        entry["chunk_seconds"] = chunk_seconds
    chunks_state = entry.setdefault("chunks", {})
    total_chunks = max(1, int(math.ceil(duration / float(chunk_seconds))))
    chunk_files = []
    for index in range(total_chunks):
        offset = index * float(chunk_seconds)
        length = min(float(chunk_seconds), max(0.1, duration - offset))
        chunk_out = os.path.join(chunk_dir, "chunk_%05d.srt" % index)
        chunk_files.append(chunk_out)
        done = chunks_state.get(str(index)) == STATUS_DONE
        if done and os.path.isfile(chunk_out) and os.path.getsize(chunk_out) > 0:
            set_file_progress(min(1.0, (offset + length) / duration))
            continue
        if op.is_cancelled():
            raise tasks.Cancelled("已取消")
        wav_path = os.path.join(chunk_dir, "chunk_%05d.wav" % index)
        for path in (wav_path, chunk_out + ".part", chunk_out):
            try:
                os.remove(path)
            except OSError:
                pass
        op.set_message("切片 %d/%d：%s" % (index + 1, total_chunks, filename))
        _extract_chunk(op, runtime, audio_path, wav_path, offset, length)
        try:
            run_worker(
                op, runtime, wav_path, chunk_out, offset,
                lambda processed, _duration, base=offset: set_file_progress(
                    min(1.0, (base + processed) / duration)),
            )
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass
        chunks_state[str(index)] = STATUS_DONE
        save_state()
        set_file_progress(min(1.0, (offset + length) / duration))

    _merge_chunk_files(chunk_files, output_path)
    shutil.rmtree(chunk_dir, ignore_errors=True)


def _process_single(op, runtime, filename, audio_path, output_path, duration,
                    set_file_progress):
    def on_processed(processed, worker_duration):
        total = duration or worker_duration
        if total:
            set_file_progress(min(1.0, processed / total))
        else:
            set_file_progress(0.5)

    run_worker(op, runtime, audio_path, output_path, 0.0, on_processed)


def _name_hash(name):
    import hashlib
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:12]


def _process_file(op, runtime, filename, entry, save_state, set_file_progress):
    audio_path = scan.audio_path(runtime["audio_root"], runtime["book_id"], filename)
    output_path = scan.srt_path(runtime["audio_root"], runtime["book_id"], filename)
    duration = entry.get("duration") or scan.refresh_duration(audio_path)
    entry["duration"] = duration
    save_state()

    options = runtime["options"]
    if (options.get("chunk_enabled", True) and duration
            and duration > float(options.get("chunk_seconds") or 1800)
            and scan.ffmpeg_path()):
        try:
            _process_chunked(
                op, runtime, filename, audio_path, output_path, float(duration),
                entry, save_state, set_file_progress)
            return
        except ChunkUnavailable as err:
            op.log("分片不可用（%s），改为整文件转写" % err)
            chunk_dir = os.path.join(
                runtime["tmp_dir"], "book_%d" % int(runtime["book_id"]),
                _name_hash(filename))
            shutil.rmtree(chunk_dir, ignore_errors=True)
            entry["chunks"] = {}
            save_state()
    _process_single(op, runtime, filename, audio_path, output_path, duration,
                    set_file_progress)


def run_transcribe(op, spec, runtime):
    """transcribe 槽位的线程入口。成功返回时 Registry 会把 op 标记完成。"""
    base = runtime["data_root"]
    runtime["options"] = dict(spec["options"])
    runtime["book_id"] = int(spec["book_id"])
    runtime["audio_root"] = spec["audio_root"]
    runtime.setdefault("tmp_dir", os.path.join(base, "tmp"))

    existing = load_job(base, spec["book_id"])
    try:
        state = prepare_state(base, spec, existing)
        save_job(base, state)
    except Exception as err:  # noqa: BLE001 - 初始化失败也要结束宿主后台任务
        op.finish(tasks.STATUS_FAILED, str(err))
        callback = runtime.get("host_finish")
        if callback:
            try:
                callback(str(err))
            except Exception:
                logging.warning("[audiobook_srt] host_finish 回调失败", exc_info=True)
        raise
    op.set_message("准备转写…")

    selected = list(spec["selected"])
    files_by_name = {item["filename"]: item for item in spec["files"]}
    total_weight = sum(max(1, files_by_name[name]["size"]) for name in selected if name in files_by_name)
    fractions = {name: 0.0 for name in selected}
    progress_lock = threading.Lock()

    def publish(message=None):
        with progress_lock:
            done = sum(
                max(1, files_by_name[name]["size"]) * fractions.get(name, 0.0)
                for name in selected if name in files_by_name
            )
            percent = (done * 100.0 / total_weight) if total_weight else 0.0
        op.set_progress(percent, message if message is not None else op.message)
        callback = runtime.get("host_update")
        if callback:
            try:
                callback(percent, op.message)
            except Exception:
                logging.warning("[audiobook_srt] host_update 回调失败", exc_info=True)

    success = skipped = failures = 0
    try:
        for index, filename in enumerate(selected):
            if op.is_cancelled():
                raise tasks.Cancelled("已取消")
            if filename not in files_by_name:
                continue
            entry = _entry(state, filename)
            if entry.get("status") in (STATUS_DONE, STATUS_SKIPPED):
                fractions[filename] = 1.0
                success += 1 if entry["status"] == STATUS_DONE else 0
                skipped += 1 if entry["status"] == STATUS_SKIPPED else 0
                publish("跳过已完成：%s" % filename)
                continue

            state["current"] = filename
            entry["status"] = STATUS_RUNNING
            entry["error"] = ""
            save_job(base, state)
            op.set_message("正在转写 %d/%d：%s" % (index + 1, len(selected), filename))
            op.log("开始：%s" % filename)
            publish()

            def set_file_progress(value):
                fractions[filename] = max(fractions.get(filename, 0.0), min(1.0, value))
                publish()

            def save_state():
                save_job(base, state)

            try:
                _process_file(op, runtime, filename, entry, save_state, set_file_progress)
                entry["status"] = STATUS_DONE
                entry["error"] = ""
                success += 1
                op.log("完成：%s" % filename)
            except tasks.Cancelled:
                entry["status"] = STATUS_PENDING
                save_job(base, state)
                raise
            except Exception as err:  # noqa: BLE001 - 单个文件失败不能拖垮整本
                entry["status"] = STATUS_FAILED
                entry["error"] = str(err)[:600]
                failures += 1
                op.log("失败：%s：%s" % (filename, err))
            fractions[filename] = 1.0
            save_job(base, state)
            publish()

        state["current"] = ""
        if failures and not success and not skipped:
            state["status"] = STATUS_FAILED
            state["error"] = "所有文件都失败"
            save_job(base, state)
            op.finish(tasks.STATUS_FAILED, "所有文件都失败，详见任务日志")
            return
        state["status"] = tasks.STATUS_COMPLETED
        state["error"] = ""
        save_job(base, state)
        if success or skipped:
            scan.delete_cached_audio_zip(spec["audio_root"], spec["book_id"])
        summary = "完成：成功 %d，跳过 %d，失败 %d" % (success, skipped, failures)
        op.set_message(summary)
        op.meta.update({"success": success, "skipped": skipped, "failed": failures})
        publish(summary)
        if failures:
            op.log(summary)
    except tasks.Cancelled:
        state["current"] = ""
        state["status"] = tasks.STATUS_CANCELLED
        save_job(base, state)
        op.finish(tasks.STATUS_CANCELLED)
    except Exception as err:  # noqa: BLE001 - 任务级异常也要落盘并结束宿主任务
        state["current"] = ""
        state["status"] = tasks.STATUS_FAILED
        state["error"] = str(err)[:600]
        save_job(base, state)
        op.finish(tasks.STATUS_FAILED, str(err))
        raise
    finally:
        callback = runtime.get("host_finish")
        if callback:
            if op.status == tasks.STATUS_CANCELLED:
                error_message = "已取消"
            elif op.status == tasks.STATUS_FAILED:
                error_message = op.error or "任务失败"
            else:
                error_message = None
            try:
                callback(error_message)
            except Exception:
                logging.warning("[audiobook_srt] host_finish 回调失败", exc_info=True)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def overwrite_for_mode(mode, explicit=None):
    if explicit is not None:
        return _as_bool(explicit)
    return str(mode or "missing").strip().lower() != "missing"


def resolve_options(raw):
    """把前端/接口参数收敛到白名单，缺省值来自 DEFAULT_OPTIONS。"""
    raw = raw or {}
    options = dict(DEFAULT_OPTIONS)
    if "model" in raw and str(raw["model"]).strip():
        options["model"] = str(raw["model"]).strip()
    if "language" in raw:
        options["language"] = str(raw["language"]).strip() or "zh"
    if "zh_script" in raw:
        value = str(raw["zh_script"]).strip().lower()
        options["zh_script"] = value if value in ("off", "t2s", "s2t") else "t2s"
    if "compute_type" in raw:
        options["compute_type"] = str(raw["compute_type"]).strip() or "int8"
    for key in ("cpu_threads", "beam_size", "chunk_seconds", "max_chars"):
        if key in raw:
            try:
                options[key] = int(raw[key])
            except (TypeError, ValueError):
                pass
    if "max_duration" in raw:
        try:
            options["max_duration"] = float(raw["max_duration"])
        except (TypeError, ValueError):
            pass
    for key in ("vad", "overwrite", "chunk_enabled"):
        if key in raw:
            options[key] = _as_bool(raw[key])
    if "initial_prompt" in raw:
        options["initial_prompt"] = str(raw["initial_prompt"] or "")[:500]
    options["cpu_threads"] = max(1, min(64, options["cpu_threads"]))
    options["beam_size"] = max(1, min(10, options["beam_size"]))
    options["chunk_seconds"] = max(60, min(6 * 3600, options["chunk_seconds"]))
    options["max_chars"] = max(12, min(120, options["max_chars"]))
    options["max_duration"] = max(2.0, min(60.0, options["max_duration"]))
    return options
