# -*- coding: utf-8 -*-
import hashlib
import importlib.util
import json
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_build():
    path = os.path.join(ROOT, "scripts", "build.py")
    spec = importlib.util.spec_from_file_location("audiobook_srt_build", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest():
    with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)


def test_manifest_fields_and_routes():
    manifest = _manifest()
    assert manifest["tool_id"] == "audiobook_srt"
    assert manifest["core_api_version"] == "1.3.0"
    assert manifest["entry_backend"] == "tool.AudiobookSrtTool"
    assert manifest["entry_frontend"] == "index.html"
    routes = manifest["api_routes"]
    assert len({route["path"] for route in routes}) == len(routes)
    source = open(os.path.join(ROOT, "backend", "tool.py"), encoding="utf-8").read()
    for route in routes:
        class_name = route["handler"].rsplit(".", 1)[1]
        assert ("class %s(" % class_name) in source
    assert "class AudiobookSrtTool(" in source


def test_backend_modules_do_not_import_host_except_entry():
    for name in ("scan.py", "srt.py", "env.py", "jobs.py", "tasks.py", "worker.py"):
        source = open(os.path.join(ROOT, "backend", name), encoding="utf-8").read()
        imports = [line.strip() for line in source.splitlines()
                   if line.strip().startswith(("import ", "from "))]
        assert not any("webserver" in line for line in imports), name


def test_locales_are_complete_enough():
    for locale in ("zh", "en", "zh-TW"):
        path = os.path.join(ROOT, "frontend", "locales", locale + ".json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        for key in ("title", "intro", "envTitle", "booksTitle", "bookId",
                    "unknownTitle", "start", "statusFailed",
                    "taskKind.transcribe", "error.unknown"):
            assert key in data, (locale, key)
    with open(os.path.join(ROOT, "frontend", "locales", "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["default"] == "zh"
    assert set(manifest["locales"]) == {"zh", "en", "zh-TW"}


def test_source_validation_passes():
    build = _load_build()
    assert build.validate_source() == []


def test_build_zip_is_reproducible_and_small(tmp_path):
    build = _load_build()
    out1, hash1, entries = build.build_zip(str(tmp_path / "dist1"))
    out2, hash2, _entries = build.build_zip(str(tmp_path / "dist2"))
    assert hash1 == hash2
    assert os.path.getsize(out1) < 1024 * 1024
    with zipfile.ZipFile(out1) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        assert "icon.png" in names
        assert "backend/requirements.lock" in names
        assert "frontend/locales/zh.json" in names
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
        assert archive.read("icon.png")[:8] == b"\x89PNG\r\n\x1a\n"
    with open(os.path.join(ROOT, "manifest.json"), "rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest()
    assert entries == build.collect_entries()
