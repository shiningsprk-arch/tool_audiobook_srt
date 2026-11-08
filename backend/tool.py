# -*- coding: utf-8 -*-
"""有声书字幕：MyBooks 外置工具后端入口与 HTTP 路由。

宿主把 manifest.json 的 api_routes 挂到 `/api/toolbox/tool/audiobook_srt/`，
并且**不注入鉴权**，所以每个 Handler 都显式写 `@js @is_admin`。

分工：
- env.py   装依赖、下模型（数据卷；工具包本体仍是纯标准库）
- scan.py  扫描宿主有声书目录
- jobs.py  转写任务、分片、断点续跑
- worker.py 独立进程里 import faster-whisper
"""
import logging
import os
import sys
import threading
import time

import tornado.escape

from webserver.handlers.base import BaseHandler, is_admin, js
from webserver.i18n import _
from webserver.toolbox.base_tool import BaseTool

from . import env
from . import jobs
from . import scan
from . import tasks

TOOL_ID = "audiobook_srt"
TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER_PATH = os.path.join(TOOL_DIR, "backend", "worker.py")
DEFAULT_AUDIO_ROOT = "/data/books/audios/"

CONFIG_DEFAULTS = dict(jobs.DEFAULT_OPTIONS)
CONFIG_DEFAULTS.update({
    "hf_endpoint": env.DEFAULT_HF_ENDPOINT,
    # 镜像默认 Aliyun：容器里 `pip config set` 可能是 root 用户配置，服务以 mybooks
    # 用户运行时不一定读得到；显式给一个国内源更稳。留空则退回 pip 自己的默认配置。
    "pip_index": "https://mirrors.aliyun.com/pypi/simple/",
})
CONFIG_KEYS = tuple(CONFIG_DEFAULTS.keys())


class AudiobookSrtTool(BaseTool):
    """有声书 SRT 字幕生成（外置工具）。"""

    service_item_name = "有声书字幕"

    @staticmethod
    def info():
        return {
            "tool_id": TOOL_ID,
            "name": "有声书字幕",
            "description": "用 faster-whisper 给有声书批量生成 SRT 字幕",
            "revision": "0.1.0",
            "author": "黏菌",
            "publish_date": "2026-11-08",
            "core_api_version": "1.3.0",
            "repo_url": "https://github.com/shiningsprk-arch/tool_audiobook_srt",
        }


def _base(tool):
    path = env.data_root(tool.TOOL_DATA_ROOT, tool.tool_id())
    env.ensure_dirs(path)
    return path


def _audio_root(tool):
    value = tool.api.settings.get("audio_output_folder", DEFAULT_AUDIO_ROOT)
    return os.path.normpath(os.path.expanduser(value or DEFAULT_AUDIO_ROOT))


def _load_config(tool):
    stored = tool.api.storage.get_config() or {}
    config = dict(CONFIG_DEFAULTS)
    for key in CONFIG_KEYS:
        if key in stored:
            config[key] = stored[key]
    return config


def _save_config(tool, updates):
    config = _load_config(tool)
    for key in CONFIG_KEYS:
        if key in updates:
            config[key] = updates[key]
    tool.api.storage.set_config(config)
    return config


def _env_state(tool, base, config=None):
    vendor = env.vendor_dir(base)
    probe = env.probe_env(sys.executable, vendor)
    meta = env.read_env_meta(vendor)
    ok, pip_text = env.pip_available(sys.executable)
    return {
        "ready": bool(probe.get("ok")),
        "error": probe.get("error") or "",
        "vendor": vendor,
        "platform": env.platform_tag(),
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "pip": ok,
        "pip_version": pip_text.splitlines()[0] if pip_text else "",
        "packages": probe.get("packages") or {},
        "installed_at": meta.get("installed_at") or "",
        "size": env.dir_size(env.deps_root(base)),
        "requirements": env.requirements_path(TOOL_DIR),
        "default_endpoint": env.DEFAULT_HF_ENDPOINT,
        "default_pip_index": "https://mirrors.aliyun.com/pypi/simple/",
    }


def _json_body(handler):
    raw = handler.request.body or b""
    if not raw:
        return {}
    content_type = handler.request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            data = tornado.escape.json_decode(raw)
        except (ValueError, TypeError) as err:
            raise ValueError("JSON 参数不合法") from err
        if not isinstance(data, dict):
            raise ValueError("JSON 参数必须是对象")
        return data
    data = {}
    for key in handler.request.arguments:
        values = handler.get_arguments(key)
        data[key] = values[0] if len(values) == 1 else values
    return data


def _error(code, message):
    return {"err": code, "msg": message}


def _job_summary(state):
    if not state:
        return None
    counts = {}
    for entry in (state.get("files") or {}).values():
        status = entry.get("status") or jobs.STATUS_PENDING
        counts[status] = counts.get(status, 0) + 1
    selected = state.get("selected") or []
    return {
        "book_id": state.get("book_id"),
        "title": state.get("title") or "",
        "status": state.get("status") or "",
        "current": state.get("current") or "",
        "updated_at": state.get("updated_at") or 0,
        "total": len(selected),
        "done": counts.get(jobs.STATUS_DONE, 0),
        "skipped": counts.get(jobs.STATUS_SKIPPED, 0),
        "failed": counts.get(jobs.STATUS_FAILED, 0),
        "pending": counts.get(jobs.STATUS_PENDING, 0),
    }


def _book_metadata(tool, ids):
    if not ids:
        return {}
    try:
        rows = tool.api.calibre.get_data_as_dict(ids=list(ids))
    except Exception as err:  # noqa: BLE001 - 元数据查不到不影响转写
        logging.warning("[audiobook_srt] 读取书籍元数据失败: %s", err)
        return {}
    result = {}
    for row in rows or []:
        book_id = row.get("id")
        if book_id is None:
            continue
        authors = row.get("authors") or []
        if isinstance(authors, str):
            authors = [part.strip() for part in authors.split("&") if part.strip()]
        result[int(book_id)] = {
            "title": row.get("title") or "",
            "authors": list(authors),
        }
    return result


def _active_transcribe():
    op = tasks.REGISTRY.get("transcribe")
    if op and op.status == tasks.STATUS_RUNNING:
        return op
    return None


class OverviewHandler(BaseHandler):
    """GET overview —— 环境、模型、配置、磁盘、当前任务。"""

    @js
    @is_admin
    async def get(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        config = _load_config(tool)
        active = _active_transcribe()
        active_book_id = (active.meta or {}).get("book_id") if active else None
        latest = jobs.latest_job(base, active_book_id)
        audio_root = _audio_root(tool)
        return {
            "err": "ok",
            "data": {
                "env": _env_state(tool, base, config),
                "models": env.list_models(env.models_root(base)),
                "config": config,
                "audio_folder": audio_root,
                "disk": {
                    "data": env.disk_usage(base),
                    "audio": env.disk_usage(audio_root),
                },
                "tasks": tasks.REGISTRY.public_all(),
                "latest_job": _job_summary(latest),
                "ffmpeg": bool(scan.ffmpeg_path()),
                "ffprobe": bool(scan.ffprobe_path()),
            },
        }


class BooksHandler(BaseHandler):
    """GET books —— 有音频的书籍列表。"""

    @js
    @is_admin
    async def get(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        audio_root = _audio_root(tool)
        books = scan.scan_books(audio_root)
        active = _active_transcribe()
        active_book_id = (active.meta or {}).get("book_id") if active else None
        metadata = _book_metadata(tool, [book["book_id"] for book in books])
        for book in books:
            meta = metadata.get(book["book_id"]) or {}
            if meta.get("title"):
                book["title"] = meta["title"]
            if meta.get("authors"):
                book["authors"] = meta["authors"]
            state = jobs.load_job(base, book["book_id"])
            if state:
                book["job"] = _job_summary(jobs.normalize_job(state, active_book_id == book["book_id"]))
        return {"err": "ok", "data": {"books": books, "audio_folder": audio_root}}


class BookHandler(BaseHandler):
    """GET book?book_id= —— 单本书文件明细与上次任务状态。"""

    @js
    @is_admin
    async def get(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        audio_root = _audio_root(tool)
        raw_id = self.get_argument("book_id", "")
        if not raw_id.isdigit():
            return _error("params.book_id", _("无效的书籍 ID"))
        book_id = int(raw_id)
        metadata = _book_metadata(tool, [book_id])
        meta = metadata.get(book_id) or {}
        try:
            book = scan.book_entry(audio_root, book_id, meta.get("title", ""), meta.get("authors"))
        except ValueError as err:
            return _error("params.book_id", str(err))
        if book["audio_count"] == 0:
            return _error("book.no_audio", _("这本书没有可处理的音频文件"))

        active = _active_transcribe()
        active_book_id = (active.meta or {}).get("book_id") if active else None
        state = jobs.load_job(base, book_id)
        if state:
            state = jobs.normalize_job(state, active_book_id == book_id)
            file_states = state.get("files") or {}
            for item in book["files"]:
                saved = file_states.get(item["filename"]) or {}
                item["job_status"] = saved.get("status") or ""
                item["job_error"] = saved.get("error") or ""
                item["duration"] = saved.get("duration") or None
        return {"err": "ok", "data": {"book": book, "job": state}}


def _install_runner(python_exe, tool_dir, vendor, pip_index):
    def target(op):
        try:
            env.install_deps(python_exe, tool_dir, vendor, pip_index, op)
        except tasks.Cancelled:
            op.finish(tasks.STATUS_CANCELLED)
        except Exception as err:  # noqa: BLE001
            op.finish(tasks.STATUS_FAILED, str(err))
    return target


class InstallHandler(BaseHandler):
    """POST install —— 后台 pip install --target 到数据卷。"""

    @js
    @is_admin
    async def post(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        if _active_transcribe():
            return _error("busy.transcribe", _("正在转写，安装依赖会替换运行环境，请先取消转写"))
        if tasks.REGISTRY.is_running("install"):
            return _error("busy.install", _("依赖正在安装中"))
        try:
            body = _json_body(self)
        except ValueError as err:
            return _error("params.invalid", str(err))
        config = _load_config(tool)
        pip_index = str(body.get("pip_index", config.get("pip_index") or "")).strip()
        vendor = env.vendor_dir(base)
        try:
            tasks.REGISTRY.start(
                "install",
                _install_runner(sys.executable, TOOL_DIR, vendor, pip_index),
                label="安装依赖",
            )
        except RuntimeError as err:
            return _error("busy.install", str(err))
        return {"err": "ok", "data": {"task": tasks.REGISTRY.get("install").to_public()}}


def _download_runner(name, base, endpoint):
    def target(op):
        try:
            env.download_model(name, env.models_root(base), endpoint, op)
        except tasks.Cancelled:
            op.finish(tasks.STATUS_CANCELLED)
        except Exception as err:  # noqa: BLE001
            op.finish(tasks.STATUS_FAILED, str(err))
    return target


class ModelDownloadHandler(BaseHandler):
    """POST model_download —— 从镜像下载模型到数据卷。"""

    @js
    @is_admin
    async def post(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        if tasks.REGISTRY.is_running("download"):
            return _error("busy.download", _("模型正在下载中"))
        try:
            body = _json_body(self)
        except ValueError as err:
            return _error("params.invalid", str(err))
        name = str(body.get("name") or "").strip()
        if name not in env.MODEL_CATALOG:
            return _error("params.model", _("未知模型"))
        if env.model_complete(env.model_dir(env.models_root(base), name)):
            return _error("model.installed", _("模型已经安装"))
        endpoint = str(body.get("endpoint") or _load_config(tool).get("hf_endpoint") or "").strip()
        try:
            endpoint = env.normalize_endpoint(endpoint)
        except env.EnvError as err:
            return _error("params.endpoint", str(err))
        try:
            tasks.REGISTRY.start(
                "download",
                _download_runner(name, base, endpoint),
                label="下载模型 %s" % name,
                meta={"model": name, "endpoint": endpoint},
            )
        except RuntimeError as err:
            return _error("busy.download", str(err))
        return {"err": "ok", "data": {"task": tasks.REGISTRY.get("download").to_public()}}


class ModelDeleteHandler(BaseHandler):
    """POST model_delete —— 删除数据卷里的模型目录。"""

    @js
    @is_admin
    async def post(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        try:
            body = _json_body(self)
        except ValueError as err:
            return _error("params.invalid", str(err))
        name = str(body.get("name") or "").strip()
        if name not in env.MODEL_CATALOG:
            return _error("params.model", _("未知模型"))
        active = _active_transcribe()
        if active and str((active.meta or {}).get("model") or "") == name:
            return _error("busy.transcribe", _("这个模型正在被转写任务使用"))
        download = tasks.REGISTRY.get("download")
        if download and download.status == tasks.STATUS_RUNNING and \
                str((download.meta or {}).get("model") or "") == name:
            return _error("busy.download", _("这个模型正在下载中"))
        try:
            removed = env.delete_model(env.models_root(base), name)
        except env.EnvError as err:
            return _error("params.model", str(err))
        return {"err": "ok", "data": {"removed": removed,
                                      "models": env.list_models(env.models_root(base))}}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


_PURGE_LOCK = threading.Lock()


class PurgeHandler(BaseHandler):
    """POST purge —— 一键卸载依赖和/或模型。

    只删数据卷里的运行环境；字幕、音频、jobs 状态、config.json 都不动。
    """

    @js
    @is_admin
    async def post(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        if _active_transcribe() or tasks.REGISTRY.is_running("install")                 or tasks.REGISTRY.is_running("download"):
            return _error("busy.purge", _("有任务在运行，请先取消或等待完成"))
        try:
            body = _json_body(self)
        except ValueError as err:
            return _error("params.invalid", str(err))
        deps = _as_bool(body.get("deps", True))
        models = _as_bool(body.get("models", True))
        if not deps and not models:
            return _error("params.purge", _("没有选择要卸载的内容"))
        if not _PURGE_LOCK.acquire(blocking=False):
            return _error("busy.purge", _("卸载正在进行中"))
        try:
            result = env.purge_runtime(base, deps=deps, models=models)
        except OSError as err:
            return _error("purge.failed", str(err))
        finally:
            _PURGE_LOCK.release()
        return {"err": "ok", "data": result}


class StartHandler(BaseHandler):
    """POST start —— 开始一本书的字幕任务。"""

    @js
    @is_admin
    async def post(self):
        tool = AudiobookSrtTool()
        base = _base(tool)
        audio_root = _audio_root(tool)
        if _active_transcribe():
            return _error("busy.transcribe", _("已有转写任务在进行"))
        if tasks.REGISTRY.is_running("install"):
            return _error("busy.install", _("依赖正在安装，请等待完成"))
        try:
            body = _json_body(self)
        except ValueError as err:
            return _error("params.invalid", str(err))

        raw_id = str(body.get("book_id") or "")
        if not raw_id.isdigit():
            return _error("params.book_id", _("无效的书籍 ID"))
        book_id = int(raw_id)
        metadata = _book_metadata(tool, [book_id])
        meta = metadata.get(book_id) or {}
        try:
            book = scan.book_entry(audio_root, book_id, meta.get("title", ""), meta.get("authors"))
        except ValueError as err:
            return _error("params.book_id", str(err))
        if book["audio_count"] == 0:
            return _error("book.no_audio", _("这本书没有可处理的音频文件"))

        mode = str(body.get("mode") or "missing")
        try:
            selected = scan.select_files(book["files"], mode, body.get("files"))
        except ValueError as err:
            return _error("params.files", str(err))
        if not selected:
            return _error("params.files.empty", _("没有需要处理的文件"))

        config = _load_config(tool)
        raw_options = dict(config)
        raw_options.update({key: body[key] for key in CONFIG_KEYS if key in body})
        options = jobs.resolve_options(raw_options)
        # 处理范围驱动覆盖语义：只补缺失→跳过已有字幕；全部/手动→重写同名 .srt。
        # 显式传 overwrite 的 API 调用仍然优先（便于脚本化）。
        if "overwrite" not in body:
            options["overwrite"] = jobs.overwrite_for_mode(mode)
        model_name = options.get("model") or env.DEFAULT_MODEL
        if model_name not in env.MODEL_CATALOG:
            return _error("params.model", _("未知模型"))
        model_path = env.model_dir(env.models_root(base), model_name)
        if not env.model_complete(model_path):
            return _error("model.missing", _("模型 %s 还没有下载完成") % model_name)
        probe = env.probe_env(sys.executable, env.vendor_dir(base))
        if not probe.get("ok"):
            return _error("deps.missing", _("运行环境未就绪：%s") % (probe.get("error") or _("请先安装依赖")))

        task_id = None
        try:
            task_id = tool.create_task(progress_data={"book_id": book_id, "title": book["title"]})
        except Exception as err:  # noqa: BLE001 - 宿主任务面板失败不阻断转写
            logging.warning("[audiobook_srt] 创建宿主后台任务失败: %s", err)

        last_update = {"value": -1.0, "at": 0.0}

        def host_update(percent, message):
            now = time.time()
            value = float(percent)
            if last_update["value"] >= 0 and int(value) == int(last_update["value"]) and now - last_update["at"] < 2.0:
                return
            last_update["value"] = value
            last_update["at"] = now
            if task_id is None:
                return
            try:
                tool.update_task_progress(
                    task_id, max(0, min(100, int(value))),
                    {"book_id": book_id, "message": message or ""},
                )
            except Exception:
                logging.warning("[audiobook_srt] 更新宿主任务进度失败", exc_info=True)

        def host_finish(error_message):
            if task_id is None:
                return
            try:
                tool.complete_task(task_id, error_message=error_message)
            except Exception:
                logging.warning("[audiobook_srt] 结束宿主任务失败", exc_info=True)

        spec = {
            "book_id": book_id,
            "title": book["title"],
            "audio_root": audio_root,
            "audio_dir": book["audio_dir"],
            "files": book["files"],
            "selected": selected,
            "options": options,
        }
        runtime = {
            "data_root": base,
            "python_exe": sys.executable,
            "vendor": env.vendor_dir(base),
            "worker_path": WORKER_PATH,
            "model_dir": model_path,
            "cwd": base,
            "host_update": host_update,
            "host_finish": host_finish,
        }
        try:
            op = tasks.REGISTRY.start(
                "transcribe",
                lambda operation: jobs.run_transcribe(operation, spec, runtime),
                label="转写字幕 %s" % book["title"],
                meta={"book_id": book_id, "title": book["title"], "model": model_name,
                      "task_id": task_id},
            )
        except RuntimeError as err:
            host_finish(str(err))
            return _error("busy.transcribe", str(err))
        return {"err": "ok", "data": {"task": op.to_public(), "selected": selected,
                                      "task_id": task_id}}


class CancelHandler(BaseHandler):
    """POST cancel —— 取消某类后台操作。"""

    @js
    @is_admin
    async def post(self):
        try:
            body = _json_body(self)
        except ValueError as err:
            return _error("params.invalid", str(err))
        kind = str(body.get("kind") or "transcribe")
        if kind not in ("transcribe", "install", "download"):
            return _error("params.kind", _("未知任务类型"))
        cancelled = tasks.REGISTRY.cancel(kind)
        if not cancelled:
            return _error("task.not_running", _("任务没有在运行"))
        return {"err": "ok", "data": {"cancelled": True}}


class TasksHandler(BaseHandler):
    """GET tasks —— 当前三类后台操作的状态。"""

    @js
    @is_admin
    async def get(self):
        return {"err": "ok", "data": {"tasks": tasks.REGISTRY.public_all()}}


class ConfigHandler(BaseHandler):
    """POST config —— 保存工具持久配置（模型、语言、参数、镜像）。"""

    @js
    @is_admin
    async def post(self):
        tool = AudiobookSrtTool()
        try:
            body = _json_body(self)
        except ValueError as err:
            return _error("params.invalid", str(err))
        updates = {}
        for key in CONFIG_KEYS:
            if key in body:
                updates[key] = body[key]
        if "hf_endpoint" in updates:
            try:
                updates["hf_endpoint"] = env.normalize_endpoint(updates["hf_endpoint"])
            except env.EnvError as err:
                return _error("params.endpoint", str(err))
        if "model" in updates and str(updates["model"]) not in env.MODEL_CATALOG:
            return _error("params.model", _("未知模型"))
        try:
            config = _save_config(tool, updates)
        except RuntimeError as err:
            return _error("config.save", str(err))
        return {"err": "ok", "data": {"config": config}}
