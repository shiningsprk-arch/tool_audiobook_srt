# -*- coding: utf-8 -*-
import os
import sys

import pytest

from backend import env


def _touch(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def test_platform_tag_shape():
    tag = env.platform_tag()
    assert tag.startswith("py")
    assert sys.platform in tag


def test_normalize_endpoint():
    assert env.normalize_endpoint("") == env.DEFAULT_HF_ENDPOINT
    assert env.normalize_endpoint("https://hf-mirror.com/") == "https://hf-mirror.com"
    with pytest.raises(env.EnvError):
        env.normalize_endpoint("ftp://example.com")


def test_model_catalog_repos():
    assert env.MODEL_CATALOG["small"]["repo"] == "Systran/faster-whisper-small"
    assert env.MODEL_CATALOG["large-v3"]["repo"] == "Systran/faster-whisper-large-v3"


def test_model_complete_requires_core_files(tmp_path):
    path = str(tmp_path / "small")
    assert env.model_complete(path) is False
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        _touch(os.path.join(path, name))
    assert env.model_complete(path) is True
    os.remove(os.path.join(path, "vocabulary.txt"))
    assert env.model_complete(path) is False
    _touch(os.path.join(path, "vocabulary.json"))
    assert env.model_complete(path) is True


def test_list_and_delete_models(tmp_path):
    root = str(tmp_path / "models")
    model = os.path.join(root, "small")
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        _touch(os.path.join(model, name), b"123")
    items = env.list_models(root)
    small = [item for item in items if item["name"] == "small"][0]
    assert small["installed"] is True
    assert small["size"] >= 12
    assert env.delete_model(root, "small") is True
    assert not os.path.isdir(model)
    with pytest.raises(env.EnvError):
        env.delete_model(root, "unknown")


def test_model_files_filter(monkeypatch):
    payload = [
        {"type": "file", "path": "config.json", "size": 10},
        {"type": "file", "path": "model.bin", "size": 100},
        {"type": "file", "path": "README.md", "size": 5},
        {"type": "file", "path": ".gitattributes", "size": 5},
        {"type": "directory", "path": "sub"},
        {"type": "file", "path": "../evil", "size": 5},
    ]
    monkeypatch.setattr(env, "_http_json", lambda url, timeout=60: payload)

    class Op(object):
        def log(self, message):
            pass

    files = env._model_files("https://hf-mirror.com", "Systran/faster-whisper-small", Op())
    assert [item["path"] for item in files] == ["config.json", "model.bin"]


def test_purge_runtime_removes_only_deps_and_models(tmp_path):
    base = str(tmp_path / "data")
    env.ensure_dirs(base)
    _touch(os.path.join(env.deps_root(base), "py312-linux-x86_64", "faster_whisper", "__init__.py"), b"x")
    _touch(os.path.join(env.models_root(base), "small", "model.bin"), b"12345")
    _touch(os.path.join(env.models_root(base), ".tiny.staging", "model.bin.part"), b"12")
    _touch(os.path.join(base, "jobs", "1.json"), b"{}")
    _touch(os.path.join(base, "tmp", "install.log"), b"log")
    config = os.path.join(base, "config.json")
    _touch(config, b"{}")

    result = env.purge_runtime(base, deps=True, models=True)
    assert result["freed"] > 0
    assert result["deps_removed"] == ["py312-linux-x86_64"]
    assert ".tiny.staging" in result["models_removed"]
    assert "small" in result["models_removed"]
    assert os.listdir(env.deps_root(base)) == []
    assert os.listdir(env.models_root(base)) == []
    assert os.path.isfile(os.path.join(base, "jobs", "1.json"))
    assert os.path.isfile(os.path.join(base, "tmp", "install.log"))
    assert os.path.isfile(config)


def test_purge_runtime_can_keep_models(tmp_path):
    base = str(tmp_path / "data")
    env.ensure_dirs(base)
    _touch(os.path.join(env.deps_root(base), "py312-linux-x86_64", "x.py"), b"x")
    _touch(os.path.join(env.models_root(base), "small", "model.bin"), b"123")
    env.purge_runtime(base, deps=True, models=False)
    assert os.listdir(env.deps_root(base)) == []
    assert os.path.isfile(os.path.join(env.models_root(base), "small", "model.bin"))


def test_av_compatible_range():
    assert env.av_compatible("16.0.1")
    assert env.av_compatible("18.1.0")
    assert not env.av_compatible("19.0.2")
    assert not env.av_compatible("15.1.0")
    assert not env.av_compatible("?")
