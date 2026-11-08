# -*- coding: utf-8 -*-
"""通用后台操作状态：每个槽位一个 Operation，线程执行，可取消。

槽位彼此独立：装依赖 / 下模型 / 转写各占一个；具体互斥规则由调用方判断。
"""
import collections
import logging
import os
import signal
import subprocess
import threading
import time
import traceback


STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


class Cancelled(RuntimeError):
    """任务被用户取消。"""


def _kill_process_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        pass

    def _force_kill():
        if proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except (OSError, ProcessLookupError):
                pass

    timer = threading.Timer(3.0, _force_kill)
    timer.daemon = True
    timer.start()


class Operation:
    def __init__(self, kind, label=""):
        self.kind = kind
        self.label = label
        self.status = STATUS_RUNNING
        self.progress = 0.0
        self.message = ""
        self.error = ""
        self.started_at = time.time()
        self.finished_at = None
        self.cancel_event = threading.Event()
        self.process = None
        self.meta = {}
        self.logs = collections.deque(maxlen=200)
        self._lock = threading.Lock()

    def log(self, message):
        text = str(message).rstrip()
        if not text:
            return
        with self._lock:
            self.logs.append(text)
        logging.info("[audiobook_srt:%s] %s", self.kind, text)

    def set_progress(self, percent, message=None):
        with self._lock:
            try:
                value = float(percent)
            except (TypeError, ValueError):
                value = 0.0
            self.progress = max(0.0, min(100.0, value))
            if message is not None:
                self.message = str(message)

    def set_message(self, message):
        with self._lock:
            self.message = str(message or "")

    def attach_process(self, proc):
        with self._lock:
            self.process = proc

    def is_cancelled(self):
        return self.cancel_event.is_set()

    def finish(self, status, error=""):
        with self._lock:
            if self.status != STATUS_RUNNING:
                return
            self.status = status
            self.error = str(error or "")
            self.finished_at = time.time()
            if status == STATUS_COMPLETED:
                self.progress = 100.0
        if status == STATUS_FAILED and error:
            logging.warning("[audiobook_srt:%s] failed: %s", self.kind, error)
        elif status == STATUS_CANCELLED:
            logging.info("[audiobook_srt:%s] cancelled", self.kind)

    def to_public(self):
        with self._lock:
            return {
                "kind": self.kind,
                "label": self.label,
                "status": self.status,
                "progress": round(self.progress, 1),
                "message": self.message,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "cancel_requested": self.cancel_event.is_set(),
                "meta": dict(self.meta),
                "logs": list(self.logs)[-60:],
            }


class Registry:
    def __init__(self):
        self._ops = {}
        self._lock = threading.Lock()

    def get(self, kind):
        with self._lock:
            return self._ops.get(kind)

    def is_running(self, kind):
        op = self.get(kind)
        return bool(op and op.status == STATUS_RUNNING)

    def public_all(self):
        with self._lock:
            return {kind: (op.to_public() if op else None) for kind, op in self._ops.items()}

    def start(self, kind, target, label="", meta=None):
        with self._lock:
            current = self._ops.get(kind)
            if current and current.status == STATUS_RUNNING:
                raise RuntimeError("已有正在进行的任务")
            op = Operation(kind, label=label)
            if meta:
                op.meta.update(meta)
            self._ops[kind] = op
        thread = threading.Thread(
            target=self._run,
            args=(op, target),
            daemon=True,
            name="audiobook_srt-%s" % kind,
        )
        thread.start()
        return op

    @staticmethod
    def _run(op, target):
        try:
            target(op)
        except Exception as err:  # noqa: BLE001 - 后台线程必须吞掉异常并落到任务状态
            logging.error("[audiobook_srt:%s] uncaught: %s\n%s",
                          op.kind, err, traceback.format_exc())
            op.finish(STATUS_FAILED, str(err))
        else:
            if op.status == STATUS_RUNNING:
                op.finish(STATUS_COMPLETED)

    def cancel(self, kind):
        op = self.get(kind)
        if not op or op.status != STATUS_RUNNING:
            return False
        op.cancel_event.set()
        op.set_message("正在取消…")
        with op._lock:  # noqa: SLF001 - Operation 与本类同模块，取消需要拿进程句柄
            proc = op.process
        _kill_process_tree(proc)
        return True


REGISTRY = Registry()


def popen_group(argv, **kwargs):
    """起一个独立进程组，便于取消时整树结束。"""
    options = dict(kwargs)
    if os.name == "posix":
        options.setdefault("start_new_session", True)
    proc = subprocess.Popen(argv, **options)
    return proc
