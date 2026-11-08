# -*- coding: utf-8 -*-
import importlib
import json
import os

from . import _host_stub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_entry_module_imports_under_host_stub():
    _host_stub.install()
    module = importlib.import_module("backend.tool")

    with open(os.path.join(ROOT, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)

    for route in manifest["api_routes"]:
        class_name = route["handler"].rsplit(".", 1)[1]
        assert hasattr(module, class_name), class_name

    tool = module.AudiobookSrtTool()
    assert tool.tool_id() == "audiobook_srt"
    assert tool.service_item_name == "有声书字幕"
