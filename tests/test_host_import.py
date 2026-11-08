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

    class FakeCalibre(object):
        def get_data_as_dict(self, ids):
            return [{"id": ids[0], "title": "书名", "authors": ["作者"]}]

    class FakeAPI(object):
        calibre = FakeCalibre()

    tool.api = FakeAPI()
    rows = tool.read_book_metadata([1])
    assert rows[0]["title"] == "书名"


def test_book_metadata_uses_cache():
    _host_stub.install()
    module = importlib.import_module("backend.tool")

    class FakeTool(object):
        def __init__(self):
            self.calls = 0

        def read_book_metadata(self, ids):
            self.calls += 1
            return [{"id": book_id, "title": "T", "authors": []} for book_id in ids]

    tool = FakeTool()
    first = module._book_metadata(tool, [999001])
    second = module._book_metadata(tool, [999001])
    assert first[999001]["title"] == "T"
    assert second[999001]["title"] == "T"
    assert tool.calls == 1
