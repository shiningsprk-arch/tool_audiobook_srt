# -*- coding: utf-8 -*-
"""运行环境与模型管理。

依赖装进数据卷的 `deps/<py-platform>/`，模型放 `models/<name>/`；
工具包本身不带 wheels、不带模型。模型下载直接用标准库走 Hugging Face 兼容镜像
（默认 hf-mirror.com），不依赖 huggingface_hub / Xet 通道，装依赖前后都能用。
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .tasks import Cancelled

DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"
USER_AGENT = "mybooks-audiobook-srt/0.1"
REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
VOCAB_FILES = ("vocabulary.txt", "vocabulary.json")
# faster-whisper 1.2.1 仍调用 av.open(..., metadata_errors="ignore")；
# PyAV 19 移除了该参数（报 open() got an unexpected keyword argument）。
AV_MIN_MAJOR = 16
AV_MAX_MAJOR = 18

MODEL_CATALOG = {
    "tiny": {
        "repo": "Systran/faster-whisper-tiny",
        "size": 75 * 1000 * 1000,
        "label": "tiny",
        "note": "只用于验证链路，质量差",
    },
    "base": {
        "repo": "Systran/faster-whisper-base",
        "size": 145 * 1000 * 1000,
        "label": "base",
        "note": "质量一般",
    },
    "small": {
        "repo": "Systran/faster-whisper-small",
        "size": 486 * 1000 * 1000,
        "label": "small",
        "note": "ARM CPU 日常推荐",
    },
    "medium": {
        "repo": "Systran/faster-whisper-medium",
        "size": 1530 * 1000 * 1000,
        "label": "medium",
        "note": "质量更好，速度更慢",
    },
    "large-v3": {
        "repo": "Systran/faster-whisper-large-v3",
        "size": 3090 * 1000 * 1000,
        "label": "large-v3",
        "note": "中文质量最好，ARM CPU 可能慢于实时",
    },
}
DEFAULT_MODEL = "small"

_CHUNK = 1024 * 1024
_probe_cache = {}
_probe_lock = threading.Lock()


class EnvError(RuntimeError):
    pass


def _check_cancel(op):
    if op is not None and op.is_cancelled():
        raise Cancelled("已取消")


def platform_tag():
    machine = (platform.machine() or "unknown").lower().replace("amd64", "x86_64")
    return "py%d%d-%s-%s" % (sys.version_info[0], sys.version_info[1], sys.platform, machine)


def data_root(tool_data_root, tool_id):
    return os.path.join(tool_data_root, tool_id)


def deps_root(base):
    return os.path.join(base, "deps")


def vendor_dir(base):
    return os.path.join(deps_root(base), platform_tag())


def models_root(base):
    return os.path.join(base, "models")


def jobs_root(base):
    return os.path.join(base, "jobs")


def tmp_root(base):
    return os.path.join(base, "tmp")


def ensure_dirs(base):
    for path in (deps_root(base), models_root(base), jobs_root(base), tmp_root(base)):
        os.makedirs(path, exist_ok=True)


def requirements_path(tool_dir):
    return os.path.join(tool_dir, "backend", "requirements.lock")


def normalize_endpoint(endpoint):
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        return DEFAULT_HF_ENDPOINT
    if not value.startswith(("http://", "https://")):
        raise EnvError("模型下载地址必须以 http:// 或 https:// 开头")
    return value


def pip_available(python_exe):
    try:
        result = subprocess.run(
            [python_exe, "-m", "pip", "--version"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    text = result.stdout.decode("utf-8", "replace").strip()
    return result.returncode == 0, text


_PROBE_CODE = r"""
import json, sys
info = {"python": sys.version.split()[0], "packages": {}}
try:
    import faster_whisper
    info["packages"]["faster-whisper"] = getattr(faster_whisper, "__version__", "?")
except Exception as err:
    info["error"] = "faster_whisper: %s" % err
try:
    import ctranslate2
    info["packages"]["ctranslate2"] = getattr(ctranslate2, "__version__", "?")
except Exception as err:
    info.setdefault("error", "ctranslate2: %s" % err)
try:
    import av
    info["packages"]["av"] = getattr(av, "__version__", "?")
except Exception as err:
    info.setdefault("error", "av: %s" % err)
try:
    import onnxruntime
    info["packages"]["onnxruntime"] = getattr(onnxruntime, "__version__", "?")
except Exception:
    pass
print(json.dumps(info, ensure_ascii=False))
"""


def av_compatible(version):
    try:
        major = int(str(version).strip().split(".", 1)[0])
    except (TypeError, ValueError):
        return False
    return AV_MIN_MAJOR <= major <= AV_MAX_MAJOR


def probe_env(python_exe, vendor, timeout=90):
    """在子进程里试 import faster_whisper；返回 {ok, ...}。"""
    if not os.path.isdir(vendor):
        return {"ok": False, "vendor": vendor, "error": "依赖目录不存在"}
    meta_path = os.path.join(vendor, "env.json")
    try:
        stat = os.stat(meta_path)
        meta_key = (stat.st_mtime, os.path.getsize(meta_path))
    except OSError:
        meta_key = (0, 0)
    try:
        dir_key = os.stat(vendor).st_mtime
    except OSError:
        dir_key = 0
    cache_key = (vendor, meta_key, dir_key, python_exe)
    with _probe_lock:
        if cache_key in _probe_cache:
            return dict(_probe_cache[cache_key])

    env = dict(os.environ)
    env["PYTHONPATH"] = vendor + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [python_exe, "-c", _PROBE_CODE],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, env=env,
        )
        output = result.stdout.decode("utf-8", "replace").strip()
        error = result.stderr.decode("utf-8", "replace").strip()
        data = json.loads(output.splitlines()[-1]) if output else {}
        if result.returncode != 0:
            data["error"] = error or ("退出码 %s" % result.returncode)
        av_version = (data.get("packages") or {}).get("av")
        if av_version and not av_compatible(av_version):
            data["error"] = (
                "PyAV/av 版本 %s 不兼容：faster-whisper 1.2.1 需要 av 16–18"
                "（av 19 移除了 metadata_errors）；请重新安装依赖" % av_version
            )
        data["ok"] = not bool(data.get("error")) and "faster-whisper" in (data.get("packages") or {})
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as err:
        data = {"ok": False, "error": str(err)}
    data["vendor"] = vendor
    with _probe_lock:
        _probe_cache[cache_key] = dict(data)
    return data


def read_env_meta(vendor):
    path = os.path.join(vendor, "env.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _write_env_meta(vendor, meta):
    with open(os.path.join(vendor, "env.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)


def _read_last_lines(path, limit=40):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        return "".join(lines[-limit:]).strip()
    except OSError:
        return ""


def install_deps(python_exe, tool_dir, vendor, pip_index, op):
    """pip install --target 到 staging，验证通过后原子换入。"""
    ok, pip_text = pip_available(python_exe)
    if not ok:
        raise EnvError("当前镜像没有可用的 pip：%s" % (pip_text or "python -m pip 失败"))

    requirements = requirements_path(tool_dir)
    if not os.path.isfile(requirements):
        raise EnvError("工具包缺少 backend/requirements.lock")

    parent = os.path.dirname(vendor)
    os.makedirs(parent, exist_ok=True)
    staging = vendor + ".installing"
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    argv = [
        python_exe, "-m", "pip", "install",
        "--target", staging,
        "--upgrade", "--ignore-installed", "--no-cache-dir",
        "--disable-pip-version-check", "--prefer-binary",
    ]
    if pip_index:
        argv.extend(["--index-url", pip_index])
    argv.extend(["-r", requirements])

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    op.log("开始安装依赖：%s" % " ".join(argv))
    if pip_index:
        op.log("pip 源：%s" % pip_index)
    else:
        op.log("pip 源：使用镜像/系统的默认配置")

    log_path = os.path.join(tmp_root(os.path.dirname(os.path.dirname(vendor))), "install.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "w", encoding="utf-8") as log_handle:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace", env=env,
                start_new_session=(os.name == "posix"),
            )
            op.attach_process(proc)
            for line in proc.stdout:
                text = line.rstrip()
                if not text:
                    continue
                log_handle.write(text + "\n")
                lower = text.lower()
                if lower.startswith("collecting"):
                    op.set_progress(10, "正在解析依赖…")
                elif lower.startswith("downloading"):
                    op.set_progress(35, "正在下载依赖包…")
                elif "installing collected packages" in lower:
                    op.set_progress(70, "正在安装依赖…")
                elif lower.startswith("successfully installed"):
                    op.set_progress(95, "依赖安装完成，正在验证…")
                if len(op.logs) == 0 or text != list(op.logs)[-1]:
                    op.log(text[:300])
                _check_cancel(op)
            returncode = proc.wait()
            op.attach_process(None)
        if op.is_cancelled():
            raise Cancelled("已取消")
        if returncode != 0:
            tail = _read_last_lines(log_path)
            raise EnvError("pip 安装失败（退出码 %s）：%s" % (returncode, tail or "无输出"))

        _check_cancel(op)
        check = probe_env(python_exe, staging, timeout=120)
        if not check.get("ok"):
            raise EnvError("依赖装好了但 import faster_whisper 失败：%s" % check.get("error"))

        meta = {
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "platform": platform_tag(),
            "python": sys.version,
            "python_exe": python_exe,
            "pip_index": pip_index or "",
            "packages": check.get("packages") or {},
        }
        _write_env_meta(staging, meta)

        old = vendor + ".old"
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(vendor):
            os.replace(vendor, old)
        os.replace(staging, vendor)
        if os.path.isdir(old):
            shutil.rmtree(old, ignore_errors=True)
        with _probe_lock:
            _probe_cache.clear()
        op.log("依赖安装完成：%s" % vendor)
        op.set_progress(100, "依赖可用")
        return meta
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def model_dir(root, name):
    return os.path.join(root, name)


def model_complete(path):
    if not os.path.isdir(path):
        return False
    for name in REQUIRED_MODEL_FILES:
        target = os.path.join(path, name)
        if not os.path.isfile(target) or os.path.getsize(target) <= 0:
            return False
    return any(os.path.isfile(os.path.join(path, name)) for name in VOCAB_FILES)


def dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def list_models(root):
    result = []
    for name, info in MODEL_CATALOG.items():
        path = model_dir(root, name)
        complete = model_complete(path)
        result.append({
            "name": name,
            "repo": info["repo"],
            "label": info["label"],
            "note": info["note"],
            "expected_size": info["size"],
            "installed": complete,
            "path": path,
            "size": dir_size(path) if os.path.isdir(path) else 0,
        })
    return result


def delete_model(root, name):
    if name not in MODEL_CATALOG:
        raise EnvError("未知模型")
    path = model_dir(root, name)
    removed = False
    if os.path.isdir(path):
        shutil.rmtree(path)
        removed = True
    staging = model_dir(root, ".%s.staging" % name)
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
        removed = True
    return removed


def _http_json(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_file(url, target, expected_size, op, on_bytes):
    part = target + ".part"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    existing = os.path.getsize(part) if os.path.isfile(part) else 0
    if expected_size and existing == expected_size:
        os.replace(part, target)
        on_bytes(existing)
        return
    if expected_size and existing > expected_size:
        os.remove(part)
        existing = 0

    headers = {"User-Agent": USER_AGENT}
    if existing:
        headers["Range"] = "bytes=%d-" % existing
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as err:
        if err.code == 416 and existing:
            try:
                os.remove(part)
            except OSError:
                pass
            return _download_file(url, target, expected_size, op, on_bytes)
        raise
    status = getattr(response, "status", 200)
    if status == 206:
        # 续传：已存在的部分也要计入总进度
        on_bytes(existing)
        mode = "ab"
    else:
        # 服务端不认 Range，整文件重下
        existing = 0
        mode = "wb"
    written = 0
    with response, open(part, mode) as handle:
        while True:
            _check_cancel(op)
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
            on_bytes(len(chunk))
    final_size = existing + written
    if expected_size and final_size != expected_size:
        raise EnvError("文件大小不符：%s（期望 %s，实际 %s）"
                       % (os.path.basename(target), expected_size, final_size))
    os.replace(part, target)


def _model_files(endpoint, repo, op):
    url = "%s/api/models/%s/tree/main?recursive=true" % (endpoint, repo)
    op.log("读取模型文件列表：%s" % url)
    tree = _http_json(url)
    files = []
    for item in tree:
        if item.get("type") != "file":
            continue
        path = item.get("path") or ""
        if not path or path.startswith(".") or path.lower().endswith(".md"):
            continue
        rel = path.replace("\\", "/")
        if rel.startswith("/") or ".." in rel.split("/"):
            continue
        files.append({"path": rel, "size": int(item.get("size") or 0)})
    if not files:
        raise EnvError("镜像没有返回可下载的模型文件")
    return files


def download_model(name, root, endpoint, op):
    if name not in MODEL_CATALOG:
        raise EnvError("未知模型")
    endpoint = normalize_endpoint(endpoint)
    repo = MODEL_CATALOG[name]["repo"]
    final = model_dir(root, name)
    if model_complete(final):
        raise EnvError("模型已安装：%s" % name)
    staging = model_dir(root, ".%s.staging" % name)
    os.makedirs(staging, exist_ok=True)

    files = _model_files(endpoint, repo, op)
    total = sum(item["size"] for item in files)
    done = 0
    for item in files:
        full = os.path.join(staging, item["path"])
        if os.path.isfile(full) and (not item["size"] or os.path.getsize(full) == item["size"]):
            done += item["size"]
            continue

    op.log("共 %d 个文件，约 %.1f MB" % (len(files), total / 1000000.0))

    def on_bytes(count):
        nonlocal done
        done += count
        if total:
            op.set_progress(done * 100.0 / total, "下载模型 %s：%.1f%%"
                            % (name, done * 100.0 / max(1, total)))

    for item in files:
        _check_cancel(op)
        target = os.path.join(staging, item["path"])
        if os.path.isfile(target) and (not item["size"] or os.path.getsize(target) == item["size"]):
            continue
        url = "%s/%s/resolve/main/%s" % (
            endpoint, repo, urllib.parse.quote(item["path"], safe="/"))
        op.set_message("下载 %s" % item["path"])
        op.log("下载 %s" % url)
        _download_file(url, target, item["size"], op, on_bytes)

    _check_cancel(op)
    if not model_complete(staging):
        raise EnvError("模型文件不完整（缺 config.json / model.bin / tokenizer.json / vocabulary）")
    with open(os.path.join(staging, "_source.json"), "w", encoding="utf-8") as handle:
        json.dump({
            "repo": repo,
            "endpoint": endpoint,
            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, handle, ensure_ascii=False, indent=2)

    old = final + ".old"
    if os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(final):
        os.replace(final, old)
    os.replace(staging, final)
    if os.path.isdir(old):
        shutil.rmtree(old, ignore_errors=True)
    op.set_progress(100, "模型可用：%s" % name)
    op.log("模型下载完成：%s" % final)
    return final


def clear_probe_cache():
    with _probe_lock:
        _probe_cache.clear()


def _remove_entry(path, is_dir):
    size = dir_size(path) if is_dir else os.path.getsize(path)
    if is_dir:
        shutil.rmtree(path)
    else:
        os.remove(path)
    return size


def purge_runtime(base, deps=True, models=True):
    """卸载数据卷里的依赖和/或模型；不动 jobs/config/音频字幕。"""
    ensure_dirs(base)
    freed = 0
    deps_removed = []
    models_removed = []
    if deps:
        root = deps_root(base)
        for entry in os.scandir(root):
            is_dir = entry.is_dir(follow_symlinks=False)
            freed += _remove_entry(entry.path, is_dir)
            deps_removed.append(entry.name)
    if models:
        root = models_root(base)
        for entry in os.scandir(root):
            is_dir = entry.is_dir(follow_symlinks=False)
            freed += _remove_entry(entry.path, is_dir)
            models_removed.append(entry.name)
    ensure_dirs(base)
    clear_probe_cache()
    return {
        "freed": freed,
        "deps_removed": sorted(deps_removed),
        "models_removed": sorted(models_removed),
    }


def disk_usage(path):
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "free": usage.free}
    except OSError:
        return {"total": 0, "free": 0}
